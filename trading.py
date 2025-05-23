#!/usr/bin/env python3
"""
Enhanced trading module with liquidity checks, multi-level TP, volume monitoring, and WebSocket support
"""
import asyncio
import threading
import time
import concurrent.futures
import websockets
import json
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any, Union
import random
import signal
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.panel import Panel
from rich.text import Text
from jupiter import execute_swap, execute_swap_async, get_quote_async
from config import Config, logger, stop_event
from solanaa import get_token_balance_async, get_token_balance, get_sol_balance
from database import db  # Import our new database module

# Rich console for beautiful CLI output
console = Console()

# Thread pool for concurrent operations
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# WebSocket connection for real-time updates
websocket_connection = None
websocket_subscriptions = {}

# Volume tracking data
volume_history = {}  # {token_address: [(timestamp, volume), ...]}

# ================ LIQUIDITY & SAFETY CHECKS ================
async def check_token_safety(token_address: str, buy_amount_sol: float) -> Tuple[bool, str, Dict]:
    """Check if token is safe to trade (liquidity, honeypot, etc.)"""
    try:
        # Check if known honeypot
        if Config.CHECK_HONEYPOT and db.is_known_honeypot(token_address):
            return False, "Token marked as honeypot/scam", {}
        
        # Get a small quote to check liquidity
        test_amount = int(0.001 * 1e9)  # 0.001 SOL
        quote = await get_quote_async(Config.SOL, token_address, test_amount)
        
        if not quote:
            return False, "No liquidity pool found", {}
        
        # Check price impact for actual buy amount
        buy_amount_lamports = int(buy_amount_sol * 1e9)
        buy_quote = await get_quote_async(Config.SOL, token_address, buy_amount_lamports)
        
        if not buy_quote:
            return False, "Failed to get buy quote", {}
        
        price_impact = float(buy_quote.get('priceImpactPct', 100))
        
        # Abort if price impact too high
        if price_impact > Config.PRICE_IMPACT_ABORT:
            return False, f"Price impact too high: {price_impact:.2f}%", {'price_impact': price_impact}
        
        # Get approximate liquidity (this is a rough estimate)
        # In production, you'd want to query the actual pool
        out_amount = float(buy_quote.get('outAmount', 0))
        if out_amount > 0:
            # Estimate liquidity based on price impact
            estimated_liquidity = (buy_amount_sol / price_impact) * 100 if price_impact > 0 else float('inf')
            
            # Check minimum liquidity
            if estimated_liquidity < Config.MIN_LIQUIDITY_USD / 100:  # Convert to SOL roughly
                return False, f"Insufficient liquidity: ~${estimated_liquidity * 100:.2f}", {'liquidity': estimated_liquidity}
            
            # Check position size relative to liquidity
            position_percent = (buy_amount_sol / estimated_liquidity) * 100
            if position_percent > Config.MAX_POSITION_SIZE_PERCENT:
                return False, f"Position too large: {position_percent:.1f}% of liquidity", {
                    'liquidity': estimated_liquidity,
                    'position_percent': position_percent
                }
        
        # All checks passed
        return True, "Token passed safety checks", {
            'price_impact': price_impact,
            'estimated_liquidity': estimated_liquidity if 'estimated_liquidity' in locals() else 0
        }
        
    except Exception as e:
        logger.error(f"Error in token safety check: {e}")
        return False, f"Safety check error: {e}", {}

def calculate_dynamic_position_size(token_address: str, base_amount: float = None) -> float:
    """Calculate position size based on volatility and other factors"""
    base_amount = base_amount or Config.BUY_AMOUNT
    
    # If we have price history, calculate volatility
    if token_address in Config.price_tracking:
        price_history = Config.price_tracking[token_address].get('price_history', [])
        if len(price_history) > 10:
            prices = [p[1] for p in price_history[-20:]]  # Last 20 data points
            if len(prices) > 1:
                # Calculate coefficient of variation (volatility)
                volatility = np.std(prices) / np.mean(prices) if np.mean(prices) > 0 else 0
                
                # Reduce position size for volatile tokens
                if volatility > 0.15:  # 15% volatility
                    return base_amount * 0.5
                elif volatility > 0.10:  # 10% volatility
                    return base_amount * 0.75
                elif volatility > 0.05:  # 5% volatility
                    return base_amount * 0.9
    
    return base_amount

# ================ ENHANCED TRADING FUNCTIONS ================
async def buy_token_async(token_address, sol_amount=None, slippage=None, source="Unknown"):
    """Enhanced buy function with safety checks"""
    # Calculate dynamic position size
    sol_amount = sol_amount or calculate_dynamic_position_size(token_address)
    slippage = slippage or Config.DEFAULT_SLIPPAGE
    
    # Safety checks
    logger.info("Performing safety checks...")
    is_safe, safety_msg, safety_data = await check_token_safety(token_address, sol_amount)
    
    if not is_safe:
        console.print(f"[bold red]⚠ SAFETY CHECK FAILED:[/] {safety_msg}")
        # Mark as potential honeypot if it's a scam
        if "honeypot" in safety_msg.lower() or "scam" in safety_msg.lower():
            db.mark_token_as_honeypot(token_address)
        return False
    
    console.print(f"[bold green]✓[/] Safety checks passed: {safety_msg}")
    if safety_data.get('price_impact'):
        console.print(f"[cyan]Price Impact:[/] {safety_data['price_impact']:.2f}%")
    
    # Convert SOL to lamports
    amount_lamports = int(sol_amount * 1e9)
    
    logger.info("BUYING TOKEN")
    logger.info(f"Token: {token_address}")
    logger.info(f"Amount: {sol_amount} SOL")
    logger.info(f"Slippage: {slippage}%")
    logger.info(f"Priority Fee: {Config.PRIORITY_FEE_MODE if Config.PRIORITY_FEE_MODE == 'auto' else f'{Config.PRIORITY_FEE_LAMPORTS} lamports'}")
    logger.info("-" * 30)
    
    # Execute swap with order splitting if enabled
    if Config.USE_ORDER_SPLITTING and safety_data.get('price_impact', 0) > Config.SPLIT_THRESHOLD_IMPACT:
        success, price_per_token = await execute_swap_with_splitting(
            Config.SOL, token_address, amount_lamports, slippage
        )
    else:
        success, price_per_token = await execute_swap_async(
            Config.SOL, token_address, amount_lamports, slippage
        )
    
    if success:
        logger.info(f"Swap transaction confirmed. Waiting for tokens to appear in wallet...")
        
        # Record buy in database
        db.record_buy(
            token_address=token_address,
            buy_price=price_per_token,
            buy_amount_sol=sol_amount,
            telegram_source=source,
            price_impact=safety_data.get('price_impact', 0)
        )
        
        # Start tracking in a separate task
        asyncio.create_task(wait_for_tokens_and_start_tracking_async(token_address, price_per_token))
        
        # Start WebSocket monitoring if enabled
        if Config.ENABLE_WEBSOCKET:
            asyncio.create_task(setup_websocket_monitoring(token_address))
        
        return True
    
    return False

async def execute_swap_with_splitting(input_mint: str, output_mint: str, amount_in: int, 
                                     slippage_percent: float = 1) -> Tuple[bool, Optional[float]]:
    """Execute swap with order splitting to reduce price impact"""
    logger.info("Using order splitting to reduce price impact...")
    
    # Check initial quote
    quote = await get_quote_async(input_mint, output_mint, amount_in)
    if not quote:
        return False, None
    
    price_impact = float(quote.get('priceImpactPct', 0))
    
    # Determine number of chunks based on impact
    if price_impact > 5.0:
        num_chunks = 4
    elif price_impact > 3.0:
        num_chunks = 3
    else:
        num_chunks = 2
    
    logger.info(f"Splitting order into {num_chunks} chunks")
    
    # Split the order
    chunk_size = amount_in // num_chunks
    results = []
    total_out = 0
    
    for i in range(num_chunks):
        # Calculate chunk amount (last chunk gets remainder)
        if i == num_chunks - 1:
            chunk_amount = amount_in - (chunk_size * (num_chunks - 1))
        else:
            chunk_amount = chunk_size
        
        logger.info(f"Executing chunk {i+1}/{num_chunks}...")
        
        # Execute chunk
        success, result = await execute_swap_async(
            input_mint, output_mint, chunk_amount, slippage_percent
        )
        
        if success:
            results.append((success, result))
            if input_mint == Config.SOL:
                # For buys, accumulate token amounts
                total_out += result if result else 0
            else:
                # For sells, accumulate SOL amounts
                total_out += result if result else 0
        else:
            logger.warning(f"Chunk {i+1} failed, continuing with remaining chunks...")
        
        # Small delay between chunks
        if i < num_chunks - 1 and success:
            await asyncio.sleep(0.5)
    
    # Check if at least one chunk succeeded
    successful_chunks = sum(1 for s, _ in results if s)
    if successful_chunks == 0:
        return False, None
    
    # Calculate average price
    if input_mint == Config.SOL:
        # For buys, calculate average price per token
        total_sol_in = sum(chunk_size if i < num_chunks-1 else amount_in - (chunk_size * (num_chunks-1)) 
                          for i in range(successful_chunks))
        avg_price = (total_sol_in / 1e9) / total_out if total_out > 0 else 0
    else:
        # For sells, return total SOL received
        avg_price = total_out
    
    logger.info(f"Order splitting complete: {successful_chunks}/{num_chunks} chunks successful")
    return True, avg_price

# ================ MULTI-LEVEL TAKE PROFIT ================
async def check_multi_level_tp(token_address: str, tracking_data: Dict) -> Tuple[bool, str, int]:
    """Check if any take profit level has been reached"""
    if not Config.ENABLE_MULTI_TP:
        return False, "Multi-TP disabled", 0
    
    percent_change = tracking_data['percent_change']
    
    # Track sold percentage in the tracking data
    if 'sold_percentage' not in tracking_data:
        tracking_data['sold_percentage'] = 0
    
    sold_so_far = tracking_data['sold_percentage']
    
    # Parse TP levels
    tp_levels = Config.parse_tp_levels()
    
    # Find the next TP level to hit
    cumulative_sold = 0
    for sell_pct, profit_target in tp_levels:
        cumulative_sold += sell_pct
        
        # If we haven't sold this level yet and price reached target
        if sold_so_far < cumulative_sold and percent_change >= profit_target:
            # Calculate how much to sell for this level
            amount_to_sell = sell_pct
            return True, f"TP Level {profit_target}% reached", amount_to_sell
    
    return False, "No TP levels hit", 0

# ================ VOLUME MONITORING ================
async def track_volume(token_address: str):
    """Track trading volume over time"""
    if token_address not in volume_history:
        volume_history[token_address] = []
    
    try:
        # This is a simplified version - in production, you'd query actual volume data
        # from the DEX or blockchain
        current_volume = await get_token_volume(token_address)
        
        if current_volume is not None:
            timestamp = datetime.now()
            volume_history[token_address].append((timestamp, current_volume))
            
            # Keep only last hour of data
            cutoff_time = timestamp - timedelta(hours=1)
            volume_history[token_address] = [
                (ts, vol) for ts, vol in volume_history[token_address]
                if ts > cutoff_time
            ]
    except Exception as e:
        logger.error(f"Error tracking volume: {e}")

async def get_token_volume(token_address: str) -> Optional[float]:
    """Get current trading volume (simplified - would query actual DEX data)"""
    # This is a placeholder - in production, you would:
    # 1. Query the DEX API for 24h volume
    # 2. Or calculate from on-chain transaction data
    # For now, we'll estimate based on price movements
    
    if token_address in Config.price_tracking:
        price_history = Config.price_tracking[token_address].get('price_history', [])
        if len(price_history) > 2:
            # Estimate volume based on price volatility (very rough estimate)
            recent_prices = [p[1] for p in price_history[-10:]]
            price_changes = [abs(recent_prices[i] - recent_prices[i-1]) for i in range(1, len(recent_prices))]
            avg_change = np.mean(price_changes) if price_changes else 0
            
            # Rough volume estimate (would be replaced with actual data)
            estimated_volume = avg_change * 10000  # Arbitrary multiplier
            return estimated_volume
    
    return None

async def check_volume_conditions(token_address: str) -> Tuple[bool, str]:
    """Check for abnormal volume patterns that might indicate dump"""
    if not Config.ENABLE_VOLUME_MONITORING or token_address not in volume_history:
        return False, "Volume monitoring disabled or no data"
    
    history = volume_history[token_address]
    if len(history) < 10:  # Need enough data points
        return False, "Insufficient volume data"
    
    # Get volumes for different time windows
    now = datetime.now()
    
    # Last 1 minute
    vol_1min = sum(vol for ts, vol in history if (now - ts).seconds <= 60)
    
    # Last 5 minutes
    vol_5min = sum(vol for ts, vol in history if (now - ts).seconds <= 300)
    
    # Last 30 minutes
    vol_30min = sum(vol for ts, vol in history if (now - ts).seconds <= 1800)
    
    # Check for volume spike (potential dump)
    if vol_5min > 0 and vol_1min > vol_5min * 0.4:  # 40% of 5min volume in 1min
        spike_ratio = vol_1min / (vol_5min / 5)  # Normalize to per-minute
        if spike_ratio > Config.VOLUME_SPIKE_MULTIPLIER:
            return True, f"Volume spike detected: {spike_ratio:.1f}x normal"
    
    # Check for volume dry-up (no interest)
    if vol_30min > 0:
        recent_avg = vol_5min / 5  # Per minute average
        overall_avg = vol_30min / 30  # Per minute average
        
        if overall_avg > 0 and recent_avg < overall_avg * (Config.VOLUME_DRYUP_PERCENT / 100):
            return True, f"Volume dried up: {(recent_avg/overall_avg)*100:.1f}% of average"
    
    return False, "Normal volume"

# ================ WEBSOCKET MONITORING ================
async def setup_websocket_monitoring(token_address: str):
    """Setup WebSocket connection for real-time price updates"""
    if not Config.ENABLE_WEBSOCKET:
        return
    
    global websocket_connection
    
    try:
        if not websocket_connection or websocket_connection.closed:
            logger.info("Establishing WebSocket connection...")
            websocket_connection = await websockets.connect(Config.WEBSOCKET_RPC)
            Config.websocket_connected = True
        
        # Subscribe to account updates for the token
        subscription_request = {
            "jsonrpc": "2.0",
            "id": f"sub_{token_address[:8]}",
            "method": "accountSubscribe",
            "params": [
                token_address,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed"
                }
            ]
        }
        
        await websocket_connection.send(json.dumps(subscription_request))
        
        # Store subscription ID when we receive it
        response = await websocket_connection.recv()
        data = json.loads(response)
        
        if 'result' in data:
            subscription_id = data['result']
            websocket_subscriptions[token_address] = subscription_id
            logger.info(f"WebSocket subscription active for {token_address}")
            
            # Start listening for updates
            asyncio.create_task(websocket_listen_for_token(token_address))
        
    except Exception as e:
        logger.error(f"WebSocket setup error: {e}")
        Config.websocket_connected = False

async def websocket_listen_for_token(token_address: str):
    """Listen for WebSocket updates for a specific token"""
    global websocket_connection
    
    while token_address in Config.price_tracking and not stop_event.is_set():
        try:
            if not websocket_connection or websocket_connection.closed:
                await asyncio.sleep(5)
                continue
            
            # Set timeout for receiving messages
            message = await asyncio.wait_for(websocket_connection.recv(), timeout=30)
            data = json.loads(message)
            
            # Process updates
            if 'params' in data and 'result' in data['params']:
                result = data['params']['result']
                
                # This would contain account data updates
                # You'd parse this to extract price-relevant information
                # For now, we'll just log that we received an update
                logger.debug(f"WebSocket update for {token_address}")
                
                # Trigger immediate price check
                asyncio.create_task(check_token_price_immediate(token_address))
                
        except asyncio.TimeoutError:
            # Normal timeout, continue listening
            continue
        except Exception as e:
            logger.error(f"WebSocket listening error: {e}")
            await asyncio.sleep(5)

async def check_token_price_immediate(token_address: str):
    """Immediately check token price (called from WebSocket updates)"""
    try:
        current_price = await check_token_price(token_address)
        if current_price and token_address in Config.price_tracking:
            tracking_data = Config.price_tracking[token_address]
            tracking_data['current_price'] = current_price
            tracking_data['last_check'] = datetime.now()
            
            # Calculate percent change
            buy_price = tracking_data['buy_price']
            percent_change = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
            tracking_data['percent_change'] = percent_change
            
            # Check sell conditions immediately
            if Config.AUTO_SELL:
                should_sell, reason, sell_percentage = await check_enhanced_sell_conditions(
                    token_address, tracking_data
                )
                if should_sell:
                    logger.info(f"WebSocket triggered sell: {reason}")
                    asyncio.create_task(sell_token_async(token_address, sell_percentage))
    except Exception as e:
        logger.error(f"Error in immediate price check: {e}")

# ================ ENHANCED SELL CONDITIONS ================
async def check_enhanced_sell_conditions(token_address: str, tracking_data: Dict) -> Tuple[bool, str, int]:
    """Enhanced sell condition checking with multi-TP and volume monitoring"""
    # First check multi-level TP
    if Config.ENABLE_MULTI_TP:
        should_sell, reason, sell_pct = await check_multi_level_tp(token_address, tracking_data)
        if should_sell:
            return should_sell, reason, sell_pct
    
    # Check volume conditions
    if Config.ENABLE_VOLUME_MONITORING:
        volume_alert, volume_reason = await check_volume_conditions(token_address)
        if volume_alert:
            return True, f"Volume alert: {volume_reason}", 100
    
    # Fall back to original sell conditions
    return check_sell_conditions(token_address, tracking_data)

# ================ ORIGINAL FUNCTIONS WITH ENHANCEMENTS ================
def buy_token(token_address, sol_amount=None, slippage=None, source="Unknown"):
    """Synchronous wrapper for enhanced buy function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            buy_token_async(token_address, sol_amount, slippage, source)
        )
    finally:
        loop.close()

async def sell_token_async(token_address, percentage=100, slippage=None):
    """Enhanced sell function with database recording"""
    # Use default slippage if not specified
    slippage = slippage or Config.DEFAULT_SLIPPAGE
    
    # Check if we're already selling this token
    if token_address in Config.auto_sell_locks and Config.auto_sell_locks[token_address]:
        console.print(f"[bold yellow]⚠[/] Already selling token {token_address}, skipping duplicate sell request")
        return False
    
    # Acquire lock
    Config.auto_sell_locks[token_address] = True
    
    try:
        if percentage < 1 or percentage > 100:
            console.print("[bold red]⚠[/] Percentage must be between 1 and 100")
            Config.auto_sell_locks[token_address] = False
            return False
        
        # Get token balance
        lamports, token_amount = await get_token_balance_async(token_address)
        if lamports == 0:
            console.print(f"[bold yellow]⚠[/] No balance found for token {token_address}")
            Config.auto_sell_locks[token_address] = False
            return False
        
        # Calculate amount to sell
        sell_amount = int(lamports * percentage / 100)
        
        # Create a nice panel for the transaction info
        sell_panel = Panel.fit(
            f"[bold cyan]Token:[/] {token_address}\n"
            f"[bold cyan]Amount:[/] {percentage}% of {token_amount} tokens\n"
            f"[bold cyan]Slippage:[/] {slippage}%\n"
            f"[bold cyan]Priority Fee:[/] {Config.PRIORITY_FEE_MODE if Config.PRIORITY_FEE_MODE == 'auto' else f'{Config.PRIORITY_FEE_LAMPORTS} lamports'}\n",
            title="[bold yellow]SELLING TOKEN[/]", 
            border_style="yellow"
        )
        console.print(sell_panel)
        
        # Get exit reason from tracking data
        exit_reason = "Manual sell"
        if token_address in Config.price_tracking:
            tracking_data = Config.price_tracking[token_address]
            _, reason, _ = await check_enhanced_sell_conditions(token_address, tracking_data)
            exit_reason = reason
        
        # Execute swap with order splitting if needed
        quote = await get_quote_async(token_address, Config.SOL, sell_amount)
        price_impact = float(quote.get('priceImpactPct', 0)) if quote else 0
        
        if Config.USE_ORDER_SPLITTING and price_impact > Config.SPLIT_THRESHOLD_IMPACT:
            success, sol_amount_received = await execute_swap_with_splitting(
                token_address, Config.SOL, sell_amount, slippage
            )
        else:
            success, sol_amount_received = await execute_swap_async(
                token_address, Config.SOL, sell_amount, slippage
            )
        
        if success:
            console.print(f"[bold green]✓[/] Sale complete! Sold {percentage}% of tokens")
            console.print(f"[bold green]✓[/] Received approximately {sol_amount_received:.6f} SOL")
            
            # Get current price for recording
            current_price = sol_amount_received / (token_amount * percentage / 100) if token_amount > 0 else 0
            
            # Record sell in database
            db.record_sell(
                token_address=token_address,
                sell_price=current_price,
                sell_amount_sol=sol_amount_received,
                exit_reason=exit_reason,
                price_impact=price_impact
            )
            
            # Update tracking data if partial sell
            if percentage < 100 and token_address in Config.price_tracking:
                tracking_data = Config.price_tracking[token_address]
                if 'sold_percentage' not in tracking_data:
                    tracking_data['sold_percentage'] = 0
                tracking_data['sold_percentage'] += percentage
            
            # If we sold 100%, remove from tracking
            if percentage == 100 and token_address in Config.price_tracking:
                # Calculate final ROI
                buy_price = Config.price_tracking[token_address]['buy_price']
                buy_amount = Config.BUY_AMOUNT  # The amount we used to buy
                roi_percent = ((sol_amount_received - buy_amount) / buy_amount) * 100
                
                # Update max ROI in database
                db.update_max_roi(token_address, roi_percent)
                
                # Create a nice panel for the results
                result_panel = Panel.fit(
                    f"[bold cyan]Initial investment:[/] {buy_amount:.6f} SOL\n"
                    f"[bold cyan]Final return:[/] {sol_amount_received:.6f} SOL\n"
                    f"[bold cyan]Profit/Loss:[/] {sol_amount_received - buy_amount:.6f} SOL\n"
                    f"[bold cyan]ROI:[/] {roi_percent:.2f}%\n",
                    title=f"[bold {'green' if roi_percent >= 0 else 'red'}]FINAL TRADE RESULT[/]", 
                    border_style=f"{'green' if roi_percent >= 0 else 'red'}"
                )
                console.print(result_panel)
                
                # Remove from tracking
                del Config.price_tracking[token_address]
                
                # Cancel WebSocket subscription
                if token_address in websocket_subscriptions:
                    # Would send unsubscribe message here
                    del websocket_subscriptions[token_address]
            
            Config.auto_sell_locks[token_address] = False
            return True
        else:
            console.print(f"[bold red]✗[/] Failed to sell token {token_address} after multiple attempts")
            
            # For critical failures like this in auto-sell, we should:
            # 1. Try one more time with higher slippage if it was a regular call
            if slippage < 5.0:
                console.print(f"[bold yellow]⚠[/] Attempting one more sale with higher slippage (5%)")
                Config.auto_sell_locks[token_address] = False  # Release lock before recursive call
                return await sell_token_async(token_address, percentage, 5.0)
                
            # 2. If it's already a high slippage attempt or a stop-loss scenario, 
            # try with a smaller percentage to get some liquidity
            elif percentage > 50 and slippage >= 5.0:
                console.print(f"[bold yellow]⚠[/] Attempting to sell 50% instead of {percentage}% to improve liquidity")
                Config.auto_sell_locks[token_address] = False
                return await sell_token_async(token_address, 50, slippage)
            
            # If all retry strategies failed, alert the user directly in the logs
            console.print(f"[bold red]✗[/] CRITICAL: Failed to sell {token_address} after all retry attempts!")
            console.print(f"[bold yellow]⚠[/] You may need to manually sell this token with higher slippage or in smaller increments")
        
        Config.auto_sell_locks[token_address] = False
        return False
    except Exception as e:
        logger.error(f"Error selling token: {e}")
        Config.auto_sell_locks[token_address] = False
        return False

def sell_token(token_address, percentage=100, slippage=None):
    """Synchronous wrapper for enhanced sell function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            sell_token_async(token_address, percentage, slippage)
        )
    finally:
        loop.close()

async def wait_for_tokens_and_start_tracking_async(token_address, price_per_token, max_attempts=20, wait_time=3):
    """Enhanced token waiting with progress indicator"""
    with Progress(
        SpinnerColumn(),
        TextColumn(f"[cyan]Waiting for {token_address} tokens to appear in wallet...[/]"),
        TimeElapsedColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Waiting...", total=max_attempts)
        
        for attempt in range(max_attempts):
            # Check for token balance
            lamports, token_amount = await get_token_balance_async(token_address)
            
            if lamports > 0 and token_amount > 0:
                # Tokens found - report success and start tracking
                console.print(f"[bold green]✓[/] Purchase complete! Received {token_amount} tokens")
                
                # Save token metadata
                db.save_token_metadata(token_address, {
                    'symbol': 'UNKNOWN',  # Would fetch actual symbol
                    'name': 'Unknown Token',  # Would fetch actual name
                    'decimals': 9,
                    'liquidity_usd': 0  # Would calculate actual liquidity
                })
                
                start_price_tracking(token_address, price_per_token)
                return
            
            # Update progress
            progress.update(task, advance=1)
            
            # Wait before checking again
            await asyncio.sleep(wait_time)
    
    # If we get here, tokens weren't found after all attempts
    console.print(f"[bold red]✗[/] Could not find tokens for {token_address} after {max_attempts} attempts")
    console.print(f"[bold yellow]⚠[/] Transaction may have failed or tokens may not be trackable")

def wait_for_tokens_and_start_tracking(token_address, price_per_token, max_attempts=20, wait_time=3):
    """Synchronous wrapper"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            wait_for_tokens_and_start_tracking_async(token_address, price_per_token, max_attempts, wait_time)
        )
    finally:
        loop.close()

# ================ ENHANCED TRACKING ================
async def track_token_price(token_address, duration_seconds):
    """Enhanced price tracking with volume monitoring and WebSocket support"""
    end_time = datetime.now().timestamp() + duration_seconds
    last_display_time = 0
    price_check_failures = 0
    
    # Start volume tracking
    if Config.ENABLE_VOLUME_MONITORING:
        asyncio.create_task(track_volume_periodic(token_address))
    
    while datetime.now().timestamp() < end_time and not stop_event.is_set():
        try:
            current_time = time.time()
            
            # Check if we have data for this token
            if token_address not in Config.price_tracking:
                logger.warning(f"No tracking data for {token_address}")
                return
            
            # If WebSocket is enabled, we might get updates from there
            # But still do periodic checks as backup
            if not Config.ENABLE_WEBSOCKET or current_time - last_display_time >= Config.PRICE_CHECK_INTERVAL:
                # Check current price
                current_price = await asyncio.to_thread(check_token_price, token_address)
                
                if current_price is not None:
                    # Reset failures on success
                    price_check_failures = 0
                    
                    # Update tracking data
                    tracking_data = Config.price_tracking[token_address]
                    tracking_data['current_price'] = current_price
                    tracking_data['last_check'] = datetime.now()
                    
                    # Add to price history
                    tracking_data['price_history'].append((datetime.now(), current_price))
                    if len(tracking_data['price_history']) > 100:
                        tracking_data['price_history'] = tracking_data['price_history'][-100:]
                    
                    # Record price point in database
                    db.record_price_point(token_address, current_price)
                    
                    # Calculate percent change from buy price
                    buy_price = tracking_data['buy_price']
                    percent_change = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
                    tracking_data['percent_change'] = percent_change
                    
                    # Update high/low prices
                    if current_price > tracking_data['high_price']:
                        tracking_data['high_price'] = current_price
                        # Update max ROI in database
                        db.update_max_roi(token_address, percent_change)
                    if current_price < tracking_data['low_price'] or tracking_data['low_price'] == 0:
                        tracking_data['low_price'] = current_price
                    
                    # Check auto-sell conditions with enhanced checks
                    if Config.AUTO_SELL:
                        should_sell, reason, sell_percentage = await check_enhanced_sell_conditions(
                            token_address, tracking_data
                        )
                        if should_sell:
                            logger.info(f"AUTO-SELL TRIGGERED: {reason}")
                            asyncio.create_task(sell_token_async(token_address, sell_percentage))
                            
                            # If selling 100%, stop tracking
                            if sell_percentage == 100:
                                return
                    
                    # Only display updates at the specified interval
                    if current_time - last_display_time >= Config.PRICE_DISPLAY_INTERVAL:
                        await display_tracking_update(token_address, tracking_data)
                        last_display_time = current_time
                else:
                    price_check_failures += 1
                    if price_check_failures > 10:
                        await asyncio.sleep(15)
            else:
                # Just wait if WebSocket is handling updates
                await asyncio.sleep(1)
                
        except Exception as e:
            price_check_failures += 1
            if price_check_failures > 5:
                logger.error(f"Error in price tracking: {e}")
        
        # Wait for next check
        await asyncio.sleep(Config.PRICE_CHECK_INTERVAL + Config.RATE_LIMIT_DELAY)
    
    logger.info(f"Completed price tracking for {token_address}")

async def track_volume_periodic(token_address: str):
    """Periodically track volume for a token"""
    while token_address in Config.price_tracking and not stop_event.is_set():
        try:
            await track_volume(token_address)
            await asyncio.sleep(30)  # Check every 30 seconds
        except Exception as e:
            logger.error(f"Error in volume tracking: {e}")
            await asyncio.sleep(60)

async def display_tracking_update(token_address: str, tracking_data: Dict):
    """Display enhanced tracking update with more information"""
    # Format time since purchase
    time_since_purchase = datetime.now() - tracking_data['buy_time']
    hours, remainder = divmod(time_since_purchase.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    time_str = f"{hours}h {minutes}m {seconds}s"
    
    # Calculate ROIs
    buy_price = tracking_data['buy_price']
    current_price = tracking_data['current_price']
    high_price = tracking_data['high_price']
    percent_change = tracking_data['percent_change']
    high_roi = ((high_price - buy_price) / buy_price) * 100
    
    # Create a nice panel for price tracking info
    roi_color = "green" if percent_change >= 0 else "red"
    
    # Build info text
    info_text = (
        f"[bold cyan]Time since purchase:[/] {time_str}\n"
        f"[bold cyan]Current price:[/] {current_price:.10f} SOL per token\n"
        f"[bold cyan]Buy price:[/] {buy_price:.10f} SOL per token\n"
        f"[bold cyan]Current ROI:[/] [bold {roi_color}]{percent_change:.2f}%[/]\n"
        f"[bold cyan]High price:[/] {high_price:.10f} (ROI: {high_roi:.2f}%)\n"
        f"[bold cyan]Low:[/] {tracking_data['low_price']:.10f}\n"
    )
    
    # Add multi-TP progress if enabled
    if Config.ENABLE_MULTI_TP and 'sold_percentage' in tracking_data:
        sold = tracking_data['sold_percentage']
        info_text += f"[bold cyan]Sold so far:[/] {sold}%\n"
    
    # Add volume info if available
    if Config.ENABLE_VOLUME_MONITORING and token_address in volume_history:
        history = volume_history[token_address]
        if history:
            recent_vol = sum(vol for ts, vol in history[-5:])
            info_text += f"[bold cyan]Recent volume:[/] {recent_vol:.2f}\n"
    
    # Add WebSocket status
    if Config.ENABLE_WEBSOCKET:
        ws_status = "Connected" if token_address in websocket_subscriptions else "Not connected"
        info_text += f"[bold cyan]WebSocket:[/] {ws_status}\n"
    
    tracking_panel = Panel.fit(
        info_text,
        title=f"[bold blue]PRICE TRACKING - {token_address}[/]", 
        border_style="blue"
    )
    console.print(tracking_panel)
    
    # Display trailing stop info if applicable
    if Config.EXIT_STRATEGY == "TRAILING_STOP" and tracking_data['trailing_stop_price'] > 0:
        trailing_stop = tracking_data['trailing_stop_price']
        exit_roi = tracking_data.get('trailing_stop_roi', 0)
        console.print(f"[cyan]TRAILING STOP:[/] {trailing_stop:.10f} (Exit ROI: {exit_roi:.2f}%)")
        console.print(f"[cyan]Trail is {Config.TRAILING_STOP} percentage points below high ROI[/]")
    
    # Display auto-sell status
    if Config.AUTO_SELL:
        _, reason, _ = await check_enhanced_sell_conditions(token_address, tracking_data)
        console.print(f"[cyan]Auto-sell status:[/] {reason}")
    
    console.print("-" * 30)

# ================ KEEP ORIGINAL FUNCTIONS ================
def start_price_tracking(token_address, buy_price):
    """Start tracking a token's price after purchase with improved analytics."""
    if token_address in Config.price_tracking_tasks and not Config.price_tracking_tasks[token_address].done():
        logger.info(f"Already tracking price for {token_address}")
        return
    
    # Initialize tracking data
    current_time = datetime.now()
    Config.price_tracking[token_address] = {
        'buy_price': buy_price,
        'current_price': buy_price,
        'buy_time': current_time,
        'last_check': current_time,
        'high_price': buy_price,
        'low_price': buy_price,
        'percent_change': 0.0,
        'trailing_stop_price': 0.0,
        'tp_reached': False,
        'highest_roi': 0.0,
        'trailing_stop_roi': 0.0,
        'price_history': [(current_time, buy_price)],
        'sold_percentage': 0  # Track partial sells for multi-TP
    }
    
    # Initialize sell lock
    Config.auto_sell_locks[token_address] = False
    
    console.print(f"[bold cyan]→[/] Starting price tracking for {token_address}")
    console.print(f"[bold cyan]→[/] Initial price: {buy_price:.10f} SOL per token")
    
    # Start tracking in a separate thread
    threading.Thread(
        target=lambda: asyncio.run(track_token_price(token_address, Config.PRICE_CHECK_DURATION)),
        daemon=True
    ).start()

async def check_token_price(token_address):
    """Check current price of a token by getting a quote."""
    try:
        # Get token balance
        lamports, token_amount = get_token_balance(token_address)
        if lamports == 0 or token_amount == 0:
            return None
        
        # Use a small amount for the quote (1% of balance)
        test_amount = max(1, int(lamports * 0.01))
        
        # Get a quote for selling that amount
        quote = execute_swap.get_quote(token_address, Config.SOL, test_amount)
        if not quote:
            return None
        
        # Calculate price per token in SOL
        input_amount = test_amount / 10 ** quote.get('inputDecimals', 9)
        output_amount = float(quote.get('outAmount', 0)) / 1e9
        
        if input_amount > 0:
            price_per_token = output_amount / input_amount
            return price_per_token
        else:
            return None
    except Exception as e:
        return None

def check_sell_conditions(token_address, tracking_data):
    """Check if the token meets any sell conditions with improved analytics."""
    if token_address not in Config.price_tracking:
        return False, "Token not tracked", 0
    
    if token_address in Config.auto_sell_locks and Config.auto_sell_locks[token_address]:
        return False, "Already selling", 0
    
    current_price = tracking_data['current_price']
    buy_price = tracking_data['buy_price']
    high_price = tracking_data['high_price']
    buy_time = tracking_data['buy_time']
    percent_change = tracking_data['percent_change']
    
    # Calculate ROIs
    high_roi = ((high_price - buy_price) / buy_price) * 100
    current_roi = percent_change
    
    # Update trailing stop calculation
    if Config.EXIT_STRATEGY == "TRAILING_STOP" and high_roi > 0:
        target_exit_roi = high_roi - Config.TRAILING_STOP
        exit_roi_price = buy_price * (1 + target_exit_roi/100)
        tracking_data['trailing_stop_roi'] = target_exit_roi
        tracking_data['trailing_stop_price'] = exit_roi_price
    
    # Update high price and trailing stop if we have a new high
    if current_price > tracking_data['high_price']:
        tracking_data['high_price'] = current_price
        tracking_data['highest_roi'] = high_roi
    
    # Check profit target based on exit strategy
    if percent_change >= Config.PROFIT_TARGET:
        if Config.EXIT_STRATEGY == "FULL_TP":
            return True, f"Profit target reached: {percent_change:.2f}% > {Config.PROFIT_TARGET}%", 100
        elif Config.EXIT_STRATEGY == "TRAILING_STOP":
            if tracking_data['trailing_stop_price'] == 0:
                target_exit_roi = percent_change - Config.TRAILING_STOP
                exit_roi_price = buy_price * (1 + target_exit_roi/100)
                tracking_data['trailing_stop_price'] = exit_roi_price
                tracking_data['trailing_stop_roi'] = target_exit_roi
                tracking_data['tp_reached'] = True
            return False, f"Profit target reached but using trailing stop only", 0
    
    # Check stop loss
    if percent_change <= Config.STOP_LOSS:
        return True, f"Stop loss triggered: {percent_change:.2f}% < {Config.STOP_LOSS}%", 100
    
    # Check trailing stop
    if Config.EXIT_STRATEGY == "TRAILING_STOP" and high_roi > 0:
        if tracking_data['trailing_stop_price'] == 0:
            target_exit_roi = high_roi - Config.TRAILING_STOP
            exit_roi_price = buy_price * (1 + target_exit_roi/100)
            tracking_data['trailing_stop_price'] = exit_roi_price
            tracking_data['trailing_stop_roi'] = target_exit_roi
        
        trailing_stop_price = tracking_data['trailing_stop_price']
        if current_price < trailing_stop_price:
            exit_roi = tracking_data.get('trailing_stop_roi', 0)
            return True, f"Trailing stop triggered: {current_price:.10f} < {trailing_stop_price:.10f} (Exit ROI: {exit_roi:.2f}%)", 100
    
    # Check max hold time
    time_held = datetime.now() - buy_time
    if time_held.total_seconds() > Config.MAX_HOLD_TIME:
        return True, f"Max hold time reached: {time_held.total_seconds()/3600:.1f} hours", 100
    
    return False, "No sell conditions met", 0

def show_active_tracking():
    """Display current price tracking information with enhanced UI and database stats."""
    # Show database statistics first
    stats = db.get_trade_statistics()
    
    if stats['closed_trades'] > 0:
        stats_panel = Panel.fit(
            f"[bold cyan]Total Trades:[/] {stats['total_trades']}\n"
            f"[bold cyan]Closed Trades:[/] {stats['closed_trades']}\n"
            f"[bold cyan]Win Rate:[/] {stats['win_rate']:.1f}%\n"
            f"[bold cyan]Total P/L:[/] {stats['total_profit_loss']:.6f} SOL\n"
            f"[bold cyan]Average ROI:[/] {stats['avg_roi']:.2f}%\n"
            f"[bold cyan]Best Trade:[/] {stats['best_trade_roi']:.2f}%\n"
            f"[bold cyan]Worst Trade:[/] {stats['worst_trade_roi']:.2f}%\n",
            title="[bold green]TRADING STATISTICS[/]",
            border_style="green"
        )
        console.print(stats_panel)
    
    if not Config.price_tracking:
        console.print("[bold yellow]No active price tracking[/]")
        return
    
    # Create a table for the tracking data
    table = Table(title="ACTIVE PRICE TRACKING", show_header=True, header_style="bold blue")
    table.add_column("Token Address", style="cyan")
    table.add_column("Time Held", style="cyan")
    table.add_column("Current Price", style="cyan")
    table.add_column("Buy Price", style="cyan")
    table.add_column("Change", style="cyan")
    table.add_column("High/Low", style="cyan")
    table.add_column("Sold", style="cyan")
    
    for token_address, data in Config.price_tracking.items():
        try:
            # Calculate time since purchase
            time_since = datetime.now() - data['buy_time']
            hours, remainder = divmod(time_since.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            time_str = f"{time_since.days}d {hours}h {minutes}m"
            
            # Format the price change
            percent_change = data['percent_change']
            change_color = "green" if percent_change >= 0 else "red"
            change_str = f"{percent_change:.2f}%"
            if percent_change > 0:
                change_str = f"+{change_str}"
            
            # Get sold percentage
            sold_pct = data.get('sold_percentage', 0)
            
            # Add row to table
            table.add_row(
                token_address,
                time_str,
                f"{data['current_price']:.10f}",
                f"{data['buy_price']:.10f}",
                f"[bold {change_color}]{change_str}[/]",
                f"H: {data['high_price']:.10f}\nL: {data['low_price']:.10f}",
                f"{sold_pct}%"
            )
        except Exception as e:
            logger.error(f"Error displaying tracking for {token_address}: {e}")
    
    console.print(table)

# Async check token price in Jupiter
async def check_token_price_in_jupiter(token_address):
    """Get the current price of a token by querying Jupiter API directly"""
    from jupiter import get_quote

    try:
        # Get token balance
        lamports, token_amount = get_token_balance(token_address)
        if lamports == 0 or token_amount == 0:
            return None

        # Use a small test amount for the quote
        test_amount = max(1, int(lamports * 0.01))
        
        # Get a quote for selling that amount
        quote = get_quote(token_address, Config.SOL, test_amount)
        if not quote:
            return None
        
        # Calculate price per token
        input_amount = test_amount / 10 ** quote.get('inputDecimals', 9)
        output_amount = float(quote.get('outAmount', 0)) / 1e9
        
        if input_amount > 0:
            price_per_token = output_amount / input_amount
            return price_per_token
        
        return None
    except Exception as e:
        logger.error(f"Error checking price in Jupiter: {e}")
        return None