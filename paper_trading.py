#!/usr/bin/env python3
"""
Paper trading module for Solana Trading Bot
- Simulates trading without using real funds
- Tracks paper portfolio balances and performance
- Measures actual RPC response times to simulate realistic delays
- Provides paper trading alternatives to real trading functions
- Persists paper trading settings to .env file
"""
import asyncio
import time
import threading
import json
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any, Union
import random
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.prompt import Prompt, Confirm

from config import Config, logger, stop_event
from solanaa import get_token_balance, get_token_balance_async, get_sol_balance, solana_client
from jupiter import get_quote, get_quote_async, execute_swap_async
from trading import check_token_price, check_sell_conditions, start_price_tracking

# Rich console for beautiful CLI output
console = Console()

# ================ PAPER TRADING CONFIGURATION ================
class PaperConfig:
    """Configuration class for paper trading settings"""
    # Default paper trading values (will be overridden by .env if available)
    PAPER_ENABLED = True
    PAPER_SOL_BALANCE = 10.0
    PAPER_BUY_AMOUNT = 1.0
    PAPER_SLIPPAGE = 1.0
    PAPER_AUTO_TRADE = True
    PAPER_AUTO_SELL = True
    
    # Exit strategy settings (inherit from main config by default)
    PAPER_PROFIT_TARGET = None  # Will use Config.PROFIT_TARGET if None
    PAPER_STOP_LOSS = None      # Will use Config.STOP_LOSS if None
    PAPER_TRAILING_STOP = None  # Will use Config.TRAILING_STOP if None
    PAPER_MAX_HOLD_TIME = None  # Will use Config.MAX_HOLD_TIME if None
    PAPER_EXIT_STRATEGY = None  # Will use Config.EXIT_STRATEGY if None
    
    # Global state for paper trading
    paper_portfolio = {
        "sol_balance": 0.0,
        "tokens": {},  # {token_address: {"amount": float, "buy_price": float, "timestamp": datetime}}
        "transaction_history": []  # List of dictionaries with transaction details
    }
    paper_processed_addresses = set()
    paper_price_tracking = {}  # Similar to Config.price_tracking but for paper trades
    paper_auto_sell_locks = {}  # Prevent multiple sells of the same token
    
    # RPC delay measurements
    rpc_delay_measurements = []
    avg_measured_delay = 100  # Default 100ms until measured
    last_delay_measurement = 0
    
    @classmethod
    def load_from_env(cls):
        """Load paper trading settings from .env file"""
        # Load paper trading enabled/disabled setting
        paper_enabled = os.getenv("PAPER_ENABLED", "true").lower()
        cls.PAPER_ENABLED = paper_enabled == "true"
        
        # Load paper trading balances and amounts
        try:
            cls.PAPER_SOL_BALANCE = float(os.getenv("PAPER_SOL_BALANCE", "10.0"))
            cls.PAPER_BUY_AMOUNT = float(os.getenv("PAPER_BUY_AMOUNT", "1.0"))
            cls.PAPER_SLIPPAGE = float(os.getenv("PAPER_SLIPPAGE", "1.0"))
        except ValueError:
            logger.error("Error parsing paper trading numerical values, using defaults")
        
        # Load auto-trading settings
        cls.PAPER_AUTO_TRADE = os.getenv("PAPER_AUTO_TRADE", "true").lower() == "true"
        cls.PAPER_AUTO_SELL = os.getenv("PAPER_AUTO_SELL", "true").lower() == "true"
        
        # Load exit strategy settings if explicitly set
        if "PAPER_PROFIT_TARGET" in os.environ:
            try:
                cls.PAPER_PROFIT_TARGET = float(os.getenv("PAPER_PROFIT_TARGET"))
            except ValueError:
                pass
                
        if "PAPER_STOP_LOSS" in os.environ:
            try:
                cls.PAPER_STOP_LOSS = float(os.getenv("PAPER_STOP_LOSS"))
            except ValueError:
                pass
                
        if "PAPER_TRAILING_STOP" in os.environ:
            try:
                cls.PAPER_TRAILING_STOP = float(os.getenv("PAPER_TRAILING_STOP"))
            except ValueError:
                pass
                
        if "PAPER_MAX_HOLD_TIME" in os.environ:
            try:
                cls.PAPER_MAX_HOLD_TIME = int(os.getenv("PAPER_MAX_HOLD_TIME"))
            except ValueError:
                pass
                
        if "PAPER_EXIT_STRATEGY" in os.environ:
            cls.PAPER_EXIT_STRATEGY = os.getenv("PAPER_EXIT_STRATEGY")
        
        # Initialize paper portfolio with the defined SOL balance
        cls.paper_portfolio["sol_balance"] = cls.PAPER_SOL_BALANCE
        
        logger.info(f"Paper trading initialized with {cls.PAPER_SOL_BALANCE} SOL balance")
    
    @classmethod
    def save_to_env(cls):
        """Save paper trading settings to .env file"""
        # Read existing .env file
        env_lines = []
        if os.path.exists('.env'):
            with open('.env', 'r') as f:
                env_lines = f.readlines()
        
        # Parse existing values
        env_values = {}
        for line in env_lines:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_values[key.strip()] = value.strip()
        
        # Update with paper trading values
        env_values['PAPER_ENABLED'] = str(cls.PAPER_ENABLED).lower()
        env_values['PAPER_SOL_BALANCE'] = str(cls.PAPER_SOL_BALANCE)
        env_values['PAPER_BUY_AMOUNT'] = str(cls.PAPER_BUY_AMOUNT)
        env_values['PAPER_SLIPPAGE'] = str(cls.PAPER_SLIPPAGE)
        env_values['PAPER_AUTO_TRADE'] = str(cls.PAPER_AUTO_TRADE).lower()
        env_values['PAPER_AUTO_SELL'] = str(cls.PAPER_AUTO_SELL).lower()
        
        # Add exit strategy settings if they differ from main config
        if cls.PAPER_PROFIT_TARGET is not None:
            env_values['PAPER_PROFIT_TARGET'] = str(cls.PAPER_PROFIT_TARGET)
        if cls.PAPER_STOP_LOSS is not None:
            env_values['PAPER_STOP_LOSS'] = str(cls.PAPER_STOP_LOSS)
        if cls.PAPER_TRAILING_STOP is not None:
            env_values['PAPER_TRAILING_STOP'] = str(cls.PAPER_TRAILING_STOP)
        if cls.PAPER_MAX_HOLD_TIME is not None:
            env_values['PAPER_MAX_HOLD_TIME'] = str(cls.PAPER_MAX_HOLD_TIME)
        if cls.PAPER_EXIT_STRATEGY is not None:
            env_values['PAPER_EXIT_STRATEGY'] = cls.PAPER_EXIT_STRATEGY
        
        # Write back to .env, maintaining comments and formatting
        new_env_lines = []
        paper_keys = set([
            'PAPER_ENABLED', 'PAPER_SOL_BALANCE', 'PAPER_BUY_AMOUNT', 
            'PAPER_SLIPPAGE', 'PAPER_AUTO_TRADE', 'PAPER_AUTO_SELL',
            'PAPER_PROFIT_TARGET', 'PAPER_STOP_LOSS', 'PAPER_TRAILING_STOP', 
            'PAPER_MAX_HOLD_TIME', 'PAPER_EXIT_STRATEGY'
        ])
        
        # Track which paper trading keys have been updated
        updated_keys = set()
        
        # First pass: update existing entries
        for line in env_lines:
            if line.strip() and not line.startswith('#') and '=' in line:
                key = line.split('=', 1)[0].strip()
                if key in paper_keys:
                    if key in env_values:
                        new_env_lines.append(f"{key}={env_values[key]}\n")
                        updated_keys.add(key)
                    else:
                        new_env_lines.append(line)
                else:
                    new_env_lines.append(line)
            else:
                new_env_lines.append(line)
        
        # Add section header if not present
        paper_section_found = False
        for line in new_env_lines:
            if '# PAPER TRADING SETTINGS' in line:
                paper_section_found = True
                break
        
        if not paper_section_found:
            new_env_lines.append("\n# PAPER TRADING SETTINGS\n")
        
        # Add any missing paper trading entries
        for key in paper_keys:
            if key in env_values and key not in updated_keys:
                new_env_lines.append(f"{key}={env_values[key]}\n")
        
        # Write back to file
        with open('.env', 'w') as f:
            f.writelines(new_env_lines)
        
        logger.info("Paper trading settings saved to .env file")
    
    @classmethod
    def update_param(cls, param_name, value):
        """Update a paper trading parameter and save to .env"""
        if hasattr(cls, param_name):
            # Convert value to appropriate type based on current value type
            current_value = getattr(cls, param_name)
            
            if isinstance(current_value, bool):
                if isinstance(value, str):
                    value = value.lower() == 'true'
            elif isinstance(current_value, int):
                value = int(value)
            elif isinstance(current_value, float):
                value = float(value)
                
            # Update the parameter
            setattr(cls, param_name, value)
            
            # If updating SOL balance, also update the portfolio
            if param_name == 'PAPER_SOL_BALANCE':
                cls.paper_portfolio["sol_balance"] = value
            
            # Save changes to .env
            cls.save_to_env()
            return True
        else:
            logger.error(f"Parameter {param_name} not found in paper configuration")
            return False

# ================ RPC DELAY MEASUREMENT ================
async def measure_rpc_delay():
    """Measure actual RPC response time using a tiny SOL transfer quote"""
    try:
        if time.time() - PaperConfig.last_delay_measurement < 300:  # Only measure every 5 minutes
            return PaperConfig.avg_measured_delay
        
        logger.info("Measuring RPC response delay...")
        
        # Use SOL as both input and output to measure a simple quote
        tiny_amount = 10000  # 0.00001 SOL in lamports
        
        start_time = time.time()
        await get_quote_async(Config.SOL, Config.SOL, tiny_amount)
        end_time = time.time()
        
        # Calculate delay in milliseconds
        delay_ms = (end_time - start_time) * 1000
        
        # Store the measurement
        PaperConfig.rpc_delay_measurements.append(delay_ms)
        # Keep only the last 10 measurements
        if len(PaperConfig.rpc_delay_measurements) > 10:
            PaperConfig.rpc_delay_measurements = PaperConfig.rpc_delay_measurements[-10:]
            
        # Calculate the average
        PaperConfig.avg_measured_delay = sum(PaperConfig.rpc_delay_measurements) / len(PaperConfig.rpc_delay_measurements)
        PaperConfig.last_delay_measurement = time.time()
        
        logger.info(f"RPC delay measured: {delay_ms:.2f}ms (avg: {PaperConfig.avg_measured_delay:.2f}ms)")
        return PaperConfig.avg_measured_delay
        
    except Exception as e:
        logger.error(f"Error measuring RPC delay: {e}")
        return PaperConfig.avg_measured_delay  # Return current average on error

# Fixed measure_rpc_delay_sync function to avoid nested event loops
def measure_rpc_delay_sync():
    """Synchronous wrapper for RPC delay measurement that won't error when run in an event loop"""
    # Check if we're already in an event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're already in an event loop, run a synchronous version directly
            try:
                # Use direct HTTP call instead of asyncio
                import requests
                import time
                
                headers = {"Content-Type": "application/json"}
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getHealth",
                    "params": []
                }
                
                start_time = time.time()
                response = requests.post(Config.RPC, headers=headers, json=payload, timeout=5)
                end_time = time.time()
                
                # Calculate delay in milliseconds
                delay_ms = (end_time - start_time) * 1000
                
                # Store the measurement
                PaperConfig.rpc_delay_measurements.append(delay_ms)
                # Keep only the last 10 measurements
                if len(PaperConfig.rpc_delay_measurements) > 10:
                    PaperConfig.rpc_delay_measurements = PaperConfig.rpc_delay_measurements[-10:]
                    
                # Calculate the average
                PaperConfig.avg_measured_delay = sum(PaperConfig.rpc_delay_measurements) / len(PaperConfig.rpc_delay_measurements)
                PaperConfig.last_delay_measurement = time.time()
                
                logger.info(f"RPC delay measured: {delay_ms:.2f}ms (avg: {PaperConfig.avg_measured_delay:.2f}ms)")
                return PaperConfig.avg_measured_delay
            except Exception as e:
                logger.error(f"Error directly measuring RPC delay: {e}")
                return PaperConfig.avg_measured_delay
        else:
            # No running event loop, we can create one
            return loop.run_until_complete(measure_rpc_delay())
    except RuntimeError:
        # If we can't get the event loop, create a new one
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(measure_rpc_delay())
        finally:
            loop.close()


# Function to show paper portfolio
def show_paper_portfolio():
    """Display the current paper trading portfolio with enhanced UI and analytics."""
    if not PaperConfig.PAPER_ENABLED:
        console.print("[bold yellow]Paper trading is disabled. Enable it in settings first.[/]")
        return
    
    # Create a panel for the Sol balance
    balance_panel = Panel.fit(
        f"[bold cyan]Current SOL Balance:[/] {PaperConfig.paper_portfolio['sol_balance']:.6f} SOL",
        title="[bold magenta]PAPER TRADING PORTFOLIO[/]", 
        border_style="magenta"
    )
    console.print(balance_panel)
    
    # Create a table for token holdings
    if PaperConfig.paper_portfolio["tokens"]:
        tokens_table = Table(show_header=True, header_style="bold magenta")
        tokens_table.add_column("Token Address", style="cyan")
        tokens_table.add_column("Amount", style="cyan", justify="right")
        tokens_table.add_column("Buy Price (SOL)", style="cyan", justify="right")
        tokens_table.add_column("Current Price (SOL)", style="cyan", justify="right")
        tokens_table.add_column("Value (SOL)", style="cyan", justify="right")
        tokens_table.add_column("ROI", style="cyan", justify="right")
        tokens_table.add_column("Time Held", style="cyan", justify="right")
        
        # Track total portfolio value
        total_value = PaperConfig.paper_portfolio["sol_balance"]
        
        # Add each token to the table
        for token_address, token_data in PaperConfig.paper_portfolio["tokens"].items():
            # Get basic token data
            token_amount = token_data["amount"]
            buy_price = token_data["buy_price"]
            buy_time = token_data.get("timestamp", datetime.now())
            
            # Get current price if available in tracking
            current_price = buy_price  # Default to buy price
            if token_address in PaperConfig.paper_price_tracking:
                current_price = PaperConfig.paper_price_tracking[token_address].get('current_price', buy_price)
            
            # Calculate value and ROI
            token_value = token_amount * current_price
            total_value += token_value
            
            roi_percent = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
            roi_text = f"{roi_percent:.2f}%"
            if roi_percent > 0:
                roi_text = f"[green]+{roi_text}[/]"
            elif roi_percent < 0:
                roi_text = f"[red]{roi_text}[/]"
                
            # Calculate time held
            time_held = datetime.now() - buy_time
            hours, remainder = divmod(time_held.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            time_held_str = f"{time_held.days}d {hours}h {minutes}m"
            
            # Add row to table
            tokens_table.add_row(
                token_address,
                f"{token_amount:.6f}",
                f"{buy_price:.10f}",
                f"{current_price:.10f}",
                f"{token_value:.6f}",
                roi_text,
                time_held_str
            )
        
        console.print(tokens_table)
        
        # Show total portfolio value
        portfolio_value_panel = Panel.fit(
            f"[bold cyan]Total Portfolio Value:[/] {total_value:.6f} SOL",
            border_style="magenta"
        )
        console.print(portfolio_value_panel)
    else:
        console.print("[yellow]No tokens in paper portfolio yet.[/]")
    
    # Show recent transaction history if available
    if PaperConfig.paper_portfolio["transaction_history"]:
        console.print("\n[bold magenta]Recent Paper Transactions:[/]")
        
        # Create a table for transactions
        tx_table = Table(show_header=True, header_style="bold magenta")
        tx_table.add_column("Type", style="cyan")
        tx_table.add_column("Token", style="cyan")
        tx_table.add_column("Amount", style="cyan", justify="right")
        tx_table.add_column("Price (SOL)", style="cyan", justify="right")
        tx_table.add_column("SOL Value", style="cyan", justify="right")
        tx_table.add_column("Time", style="cyan", justify="right")
        
        # Show the 5 most recent transactions
        recent_txs = PaperConfig.paper_portfolio["transaction_history"][-5:]
        for tx in recent_txs:
            # Format transaction type
            tx_type = "[green]BUY[/]" if tx["type"] == "buy" else "[yellow]SELL[/]"
            
            # Format SOL amount
            if tx["type"] == "buy":
                sol_value = f"-{tx['sol_spent']:.6f}"
            else:
                sol_value = f"+{tx['sol_received']:.6f}"
            
            # Format timestamp
            if isinstance(tx["timestamp"], str):
                tx_time = datetime.fromisoformat(tx["timestamp"]).strftime("%H:%M:%S")
            else:
                tx_time = tx["timestamp"].strftime("%H:%M:%S")
            
            # Add row to table
            tx_table.add_row(
                tx_type,
                tx["token"][:10] + "...",
                f"{tx['amount']:.6f}",
                f"{tx['price']:.10f}",
                sol_value,
                tx_time
            )
        
        console.print(tx_table)
    
    # Show performance stats if trading has been done
    if len(PaperConfig.paper_portfolio["transaction_history"]) > 0:
        # Calculate basic stats
        total_buys = len([tx for tx in PaperConfig.paper_portfolio["transaction_history"] if tx["type"] == "buy"])
        total_sells = len([tx for tx in PaperConfig.paper_portfolio["transaction_history"] if tx["type"] == "sell"])
        
        # For completed trades (buy+sell), calculate profit/loss
        profits = []
        buy_costs = {}
        
        # Track sell amounts for partial sells
        sell_amounts = {}
        
        # Process buys first
        for tx in PaperConfig.paper_portfolio["transaction_history"]:
            if tx["type"] == "buy":
                token = tx["token"]
                if token not in buy_costs:
                    buy_costs[token] = 0
                    sell_amounts[token] = 0
                buy_costs[token] += tx["sol_spent"]
        
        # Then process sells to calculate profits
        for tx in PaperConfig.paper_portfolio["transaction_history"]:
            if tx["type"] == "sell":
                token = tx["token"]
                if token in buy_costs:
                    # Calculate fraction of position sold
                    token_data = next((data for addr, data in PaperConfig.paper_portfolio["tokens"].items() 
                                      if addr == token), None)
                    total_amount = tx["amount"] + (token_data["amount"] if token_data else 0)
                    fraction_sold = tx["amount"] / total_amount if total_amount > 0 else 1
                    
                    # Calculate cost basis for this portion
                    cost_basis = buy_costs[token] * fraction_sold
                    
                    # Calculate profit/loss
                    profit = tx["sol_received"] - cost_basis
                    profits.append(profit)
        
        # Calculate stats
        total_profit = sum(profits) if profits else 0
        avg_profit = total_profit / len(profits) if profits else 0
        win_count = len([p for p in profits if p > 0])
        loss_count = len([p for p in profits if p <= 0])
        win_rate = (win_count / len(profits) * 100) if profits else 0
        
        # Create a statistics panel
        stats_panel = Panel.fit(
            f"[bold cyan]Total Profit/Loss:[/] {'[green]' if total_profit >= 0 else '[red]'}{total_profit:.6f}[/] SOL\n"
            f"[bold cyan]Trades Made:[/] {total_buys} buys, {total_sells} sells\n"
            f"[bold cyan]Completed Trades:[/] {len(profits)}\n"
            f"[bold cyan]Win Rate:[/] {win_rate:.1f}% ({win_count} wins, {loss_count} losses)",
            title="[bold magenta]PAPER TRADING STATISTICS[/]",
            border_style="magenta"
        )
        console.print(stats_panel)

# ================ PAPER TRADING FUNCTIONS ================
async def paper_buy_token_async(token_address, sol_amount=None, slippage=None):
    """Paper trading version of buy_token that simulates a purchase without real funds"""
    # Use config values if not specified
    sol_amount = sol_amount or PaperConfig.PAPER_BUY_AMOUNT
    slippage = slippage or PaperConfig.PAPER_SLIPPAGE
    
    # Measure RPC delay for realistic simulation
    delay_ms = await measure_rpc_delay()
    
    # Check if we have enough paper SOL
    if sol_amount > PaperConfig.paper_portfolio["sol_balance"]:
        console.print(f"[bold red]⚠[/] Insufficient paper SOL balance: {PaperConfig.paper_portfolio['sol_balance']} SOL")
        console.print(f"[bold red]⚠[/] Required for trade: {sol_amount} SOL")
        return False
    
    # Convert SOL to lamports for the API
    amount_lamports = int(sol_amount * 1e9)
    
    logger.info("PAPER BUYING TOKEN")
    logger.info(f"Token: {token_address}")
    logger.info(f"Amount: {sol_amount} SOL")
    logger.info(f"Slippage: {slippage}%")
    logger.info("-" * 30)
    
    # Create a progress spinner for the simulated transaction
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]Simulating paper buy transaction...[/]"),
        TimeElapsedColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Simulating...", total=None)
        
        # Get a real price quote first
        quote = await get_quote_async(Config.SOL, token_address, amount_lamports, int(slippage * 100))
        
        if not quote:
            logger.error("Failed to get quote for paper trade")
            return False
        
        # Calculate the token amount we would receive
        token_amount = float(quote.get('outAmount', 0)) / 10**quote.get('outputDecimals', 9)
        token_price = sol_amount / token_amount if token_amount > 0 else 0
        
        # Simulate transaction delay
        await asyncio.sleep(delay_ms / 1000)  # Convert ms to seconds
        
        # Calculate approximate gas fees (0.000005 - 0.00001 SOL)
        gas_fee = random.uniform(0.000005, 0.00001)
        
        # Update paper portfolio
        PaperConfig.paper_portfolio["sol_balance"] -= (sol_amount + gas_fee)
        
        # Add token to portfolio
        if token_address not in PaperConfig.paper_portfolio["tokens"]:
            PaperConfig.paper_portfolio["tokens"][token_address] = {
                "amount": token_amount,
                "buy_price": token_price,
                "timestamp": datetime.now()
            }
        else:
            # If already have some tokens, calculate new average price
            existing = PaperConfig.paper_portfolio["tokens"][token_address]
            total_amount = existing["amount"] + token_amount
            avg_price = (existing["amount"] * existing["buy_price"] + token_amount * token_price) / total_amount
            PaperConfig.paper_portfolio["tokens"][token_address] = {
                "amount": total_amount,
                "buy_price": avg_price,
                "timestamp": datetime.now()
            }
        
        # Add to transaction history
        PaperConfig.paper_portfolio["transaction_history"].append({
            "type": "buy",
            "token": token_address,
            "amount": token_amount,
            "price": token_price,
            "sol_spent": sol_amount,
            "gas_fee": gas_fee,
            "timestamp": datetime.now().isoformat()
        })
        
        # Mark as processed
        PaperConfig.paper_processed_addresses.add(token_address)
    
    # Start price tracking
    await start_paper_price_tracking(token_address, token_price)
    
    # Show success message
    console.print(f"[bold green]✓[/] Paper purchase successful!")
    console.print(f"[bold green]✓[/] Received {token_amount:.6f} tokens at {token_price:.10f} SOL per token")
    console.print(f"[bold green]✓[/] Remaining paper balance: {PaperConfig.paper_portfolio['sol_balance']:.6f} SOL")
    
    return True

def paper_buy_token(token_address, sol_amount=None, slippage=None):
    """Synchronous wrapper for paper buy function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(paper_buy_token_async(token_address, sol_amount, slippage))
    finally:
        loop.close()

async def paper_sell_token_async(token_address, percentage=100, slippage=None):
    """Paper trading version of sell_token that simulates a sale without real funds"""
    # Use default slippage if not specified
    slippage = slippage or PaperConfig.PAPER_SLIPPAGE
    
    # Measure RPC delay for realistic simulation
    delay_ms = await measure_rpc_delay()
    
    # Check if we're already selling this token
    if token_address in PaperConfig.paper_auto_sell_locks and PaperConfig.paper_auto_sell_locks[token_address]:
        console.print(f"[bold yellow]⚠[/] Already paper selling token {token_address}, skipping duplicate sell request")
        return False
    
    # Acquire lock
    PaperConfig.paper_auto_sell_locks[token_address] = True
    
    try:
        if percentage < 1 or percentage > 100:
            console.print("[bold red]⚠[/] Percentage must be between 1 and 100")
            PaperConfig.paper_auto_sell_locks[token_address] = False
            return False
        
        # Check if we have this token
        if token_address not in PaperConfig.paper_portfolio["tokens"]:
            console.print(f"[bold yellow]⚠[/] No paper balance found for token {token_address}")
            PaperConfig.paper_auto_sell_locks[token_address] = False
            return False
        
        token_data = PaperConfig.paper_portfolio["tokens"][token_address]
        token_amount = token_data["amount"]
        buy_price = token_data["buy_price"]
        
        # Calculate amount to sell
        amount_to_sell = token_amount * percentage / 100
        
        # Create a nice panel for the transaction info
        sell_panel = Panel.fit(
            f"[bold cyan]Token:[/] {token_address}\n"
            f"[bold cyan]Amount:[/] {percentage}% of {token_amount:.6f} tokens\n"
            f"[bold cyan]Slippage:[/] {slippage}%\n",
            title="[bold yellow]PAPER SELLING TOKEN[/]", 
            border_style="yellow"
        )
        console.print(sell_panel)
        
        # Create a progress spinner for the simulated transaction
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Simulating paper sell transaction...[/]"),
            TimeElapsedColumn(),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Simulating...", total=None)
            
            # Convert token amount to lamports for the API
            token_decimals = 9  # Default for most SPL tokens
            token_lamports = int(amount_to_sell * (10 ** token_decimals))
            
            # Get real price quote
            quote = await get_quote_async(token_address, Config.SOL, token_lamports, int(slippage * 100))
            
            if not quote:
                logger.error("Failed to get quote for paper sell trade")
                PaperConfig.paper_auto_sell_locks[token_address] = False
                return False
            
            # Calculate SOL we would receive
            sol_amount_received = float(quote.get('outAmount', 0)) / 1e9
            current_token_price = sol_amount_received / amount_to_sell if amount_to_sell > 0 else 0
            
            # Simulate transaction delay
            await asyncio.sleep(delay_ms / 1000)  # Convert ms to seconds
            
            # Calculate approximate gas fees (0.000005 - 0.00001 SOL)
            gas_fee = random.uniform(0.000005, 0.00001)
            
            # Calculate net SOL received after gas
            net_sol_received = sol_amount_received - gas_fee
            
            # Update paper portfolio
            PaperConfig.paper_portfolio["sol_balance"] += net_sol_received
            
            # Update token amount
            if percentage == 100:
                # Sold everything, remove token
                del PaperConfig.paper_portfolio["tokens"][token_address]
            else:
                # Sold partial amount
                remaining_amount = token_amount - amount_to_sell
                PaperConfig.paper_portfolio["tokens"][token_address]["amount"] = remaining_amount
            
            # Add to transaction history
            PaperConfig.paper_portfolio["transaction_history"].append({
                "type": "sell",
                "token": token_address,
                "amount": amount_to_sell,
                "price": current_token_price,
                "sol_received": sol_amount_received,
                "gas_fee": gas_fee,
                "timestamp": datetime.now().isoformat()
            })
        
        # Display results
        console.print(f"[bold green]✓[/] Paper sale complete! Sold {amount_to_sell:.6f} tokens")
        console.print(f"[bold green]✓[/] Received approximately {net_sol_received:.6f} SOL")
        
        # Calculate ROI 
        initial_investment = amount_to_sell * buy_price
        roi_percent = ((net_sol_received - initial_investment) / initial_investment) * 100 if initial_investment > 0 else 0
        
        # Create a nice panel for the results
        result_panel = Panel.fit(
            f"[bold cyan]Initial investment:[/] {initial_investment:.6f} SOL\n"
            f"[bold cyan]Final return:[/] {net_sol_received:.6f} SOL\n"
            f"[bold cyan]Profit/Loss:[/] {net_sol_received - initial_investment:.6f} SOL\n"
            f"[bold cyan]ROI:[/] {roi_percent:.2f}%\n",
            title=f"[bold {'green' if roi_percent >= 0 else 'red'}]PAPER TRADE RESULT[/]", 
            border_style=f"{'green' if roi_percent >= 0 else 'red'}"
        )
        console.print(result_panel)
        
        # If we sold 100%, clean up price tracking
        if percentage == 100 and token_address in PaperConfig.paper_price_tracking:
            del PaperConfig.paper_price_tracking[token_address]
        
        PaperConfig.paper_auto_sell_locks[token_address] = False
        return True
    
    except Exception as e:
        logger.error(f"Error in paper sell: {e}")
        PaperConfig.paper_auto_sell_locks[token_address] = False
        return False

def paper_sell_token(token_address, percentage=100, slippage=None):
    """Synchronous wrapper for paper sell function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(paper_sell_token_async(token_address, percentage, slippage))
    finally:
        loop.close()

# ================ PAPER PRICE TRACKING ================
async def check_paper_token_price(token_address):
    """Check current price of a token for paper trading"""
    try:
        # Check if token exists in paper portfolio
        if token_address not in PaperConfig.paper_portfolio["tokens"]:
            return None
        
        token_data = PaperConfig.paper_portfolio["tokens"][token_address]
        token_amount = token_data["amount"]
        
        # Convert to lamports for the API
        token_decimals = 9  # Default for most SPL tokens
        token_lamports = int(token_amount * (10 ** token_decimals))
        
        # Get a small test amount (1% of holdings)
        test_amount = max(1, int(token_lamports * 0.01))
        
        # Get a quote for selling that amount
        quote = await get_quote_async(token_address, Config.SOL, test_amount)
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
        logger.debug(f"Error in paper price check: {e}")
        return None

async def start_paper_price_tracking(token_address, buy_price):
    """Start tracking a token's price for paper trading"""
    # Check if already tracking
    if token_address in PaperConfig.paper_price_tracking:
        logger.info(f"Already tracking paper price for {token_address}")
        return
    
    # Initialize tracking data
    current_time = datetime.now()
    PaperConfig.paper_price_tracking[token_address] = {
        'buy_price': buy_price,
        'current_price': buy_price,
        'buy_time': current_time,
        'last_check': current_time,
        'high_price': buy_price,
        'low_price': buy_price,
        'percent_change': 0.0,
        'trailing_stop_price': 0.0,  # Initialize trailing stop
        'tp_reached': False,         # Flag to track if TP has been reached
        'highest_roi': 0.0,          # Track the highest ROI percentage we've seen
        'trailing_stop_roi': 0.0,    # ROI at trailing stop
        'price_history': [(current_time, buy_price)]  # Add price history tracking
    }
    
    # Initialize sell lock
    PaperConfig.paper_auto_sell_locks[token_address] = False
    
    console.print(f"[bold cyan]→[/] Starting paper price tracking for {token_address}")
    console.print(f"[bold cyan]→[/] Initial price: {buy_price:.10f} SOL per token")
    
    # Start tracking in a separate thread
    threading.Thread(
        target=lambda: asyncio.run(track_paper_token_price(token_address, Config.PRICE_CHECK_DURATION)),
        daemon=True
    ).start()

async def track_paper_token_price(token_address, duration_seconds):
    """Track a token's price for paper trading"""
    end_time = datetime.now().timestamp() + duration_seconds
    last_display_time = 0
    price_check_failures = 0
    
    while datetime.now().timestamp() < end_time and not stop_event.is_set():
        try:
            current_time = time.time()
            
            # Check if we have data for this token
            if token_address not in PaperConfig.paper_price_tracking:
                logger.warning(f"No paper tracking data for {token_address}")
                return
            
            # Check if token still exists in portfolio
            if token_address not in PaperConfig.paper_portfolio["tokens"]:
                logger.info(f"Token {token_address} no longer in paper portfolio, stopping tracking")
                if token_address in PaperConfig.paper_price_tracking:
                    del PaperConfig.paper_price_tracking[token_address]
                return
            
            # Check current price
            current_price = await check_paper_token_price(token_address)
            
            if current_price is not None:
                # Reset failures on success
                price_check_failures = 0
                
                # Update tracking data
                tracking_data = PaperConfig.paper_price_tracking[token_address]
                tracking_data['current_price'] = current_price
                tracking_data['last_check'] = datetime.now()
                
                # Add to price history (keep last 100 data points for charting)
                tracking_data['price_history'].append((datetime.now(), current_price))
                if len(tracking_data['price_history']) > 100:
                    tracking_data['price_history'] = tracking_data['price_history'][-100:]
                
                # Calculate percent change from buy price
                buy_price = tracking_data['buy_price']
                percent_change = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
                tracking_data['percent_change'] = percent_change
                
                # Update high/low prices
                if current_price > tracking_data['high_price']:
                    tracking_data['high_price'] = current_price
                if current_price < tracking_data['low_price'] or tracking_data['low_price'] == 0:
                    tracking_data['low_price'] = current_price
                
                # Get exit strategy settings - use paper-specific if set, otherwise use main config
                profit_target = PaperConfig.PAPER_PROFIT_TARGET if PaperConfig.PAPER_PROFIT_TARGET is not None else Config.PROFIT_TARGET
                stop_loss = PaperConfig.PAPER_STOP_LOSS if PaperConfig.PAPER_STOP_LOSS is not None else Config.STOP_LOSS
                trailing_stop = PaperConfig.PAPER_TRAILING_STOP if PaperConfig.PAPER_TRAILING_STOP is not None else Config.TRAILING_STOP
                max_hold_time = PaperConfig.PAPER_MAX_HOLD_TIME if PaperConfig.PAPER_MAX_HOLD_TIME is not None else Config.MAX_HOLD_TIME
                exit_strategy = PaperConfig.PAPER_EXIT_STRATEGY if PaperConfig.PAPER_EXIT_STRATEGY is not None else Config.EXIT_STRATEGY
                
                # Check auto-sell conditions if enabled
                if PaperConfig.PAPER_AUTO_SELL:
                    # Update high price and trailing stop calculation (same logic as in trading.py)
                    high_price = tracking_data['high_price']
                    high_roi = ((high_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
                    
                    # Calculate trailing stop based on percentage points from high ROI
                    if exit_strategy == "TRAILING_STOP" and high_roi > 0:
                        target_exit_roi = high_roi - trailing_stop
                        exit_roi_price = buy_price * (1 + target_exit_roi/100)
                        tracking_data['trailing_stop_roi'] = target_exit_roi
                        tracking_data['trailing_stop_price'] = exit_roi_price
                    
                    # Check sell conditions
                    should_sell = False
                    reason = "No sell conditions met"
                    sell_percentage = 0
                    
                    # Check profit target
                    if percent_change >= profit_target:
                        if exit_strategy == "FULL_TP":
                            should_sell = True
                            reason = f"Profit target reached: {percent_change:.2f}% > {profit_target}%"
                            sell_percentage = 100
                        elif exit_strategy == "TRAILING_STOP":
                            # Set trailing stop if not set yet
                            if tracking_data['trailing_stop_price'] == 0:
                                target_exit_roi = percent_change - trailing_stop
                                exit_roi_price = buy_price * (1 + target_exit_roi/100)
                                tracking_data['trailing_stop_price'] = exit_roi_price
                                tracking_data['trailing_stop_roi'] = target_exit_roi
                                tracking_data['tp_reached'] = True
                                logger.info(f"Setting initial paper trailing stop at {exit_roi_price:.10f}")
                    
                    # Check stop loss
                    if percent_change <= stop_loss:
                        should_sell = True
                        reason = f"Stop loss triggered: {percent_change:.2f}% < {stop_loss}%"
                        sell_percentage = 100
                    
                    # Check trailing stop
                    if exit_strategy == "TRAILING_STOP" and high_roi > 0:
                        # Initialize trailing stop if not set
                        if tracking_data['trailing_stop_price'] == 0:
                            target_exit_roi = high_roi - trailing_stop
                            exit_roi_price = buy_price * (1 + target_exit_roi/100)
                            tracking_data['trailing_stop_price'] = exit_roi_price
                            tracking_data['trailing_stop_roi'] = target_exit_roi
                        
                        # Check if current price fell below trailing stop
                        trailing_stop_price = tracking_data['trailing_stop_price']
                        if current_price < trailing_stop_price:
                            exit_roi = tracking_data.get('trailing_stop_roi', 0)
                            should_sell = True
                            reason = f"Trailing stop triggered: {current_price:.10f} < {trailing_stop_price:.10f} (Exit ROI: {exit_roi:.2f}%)"
                            sell_percentage = 100
                    
                    # Check max hold time
                    time_held = datetime.now() - tracking_data['buy_time']
                    if time_held.total_seconds() > max_hold_time:
                        should_sell = True
                        reason = f"Max hold time reached: {time_held.total_seconds()/3600:.1f} hours"
                        sell_percentage = 100
                    
                    # Execute sell if conditions met
                    if should_sell:
                        logger.info(f"PAPER AUTO-SELL TRIGGERED: {reason}")
                        
                        # Create a proper async sell task
                        try:
                            asyncio.create_task(paper_sell_token_async(token_address, sell_percentage))
                        except Exception as e:
                            logger.error(f"Error initiating paper sell task: {e}")
                        
                        # If selling 100%, stop tracking this token
                        if sell_percentage == 100:
                            return
                
                # Only display updates at the specified interval
                if current_time - last_display_time >= Config.PRICE_DISPLAY_INTERVAL:
                    # Format time since purchase
                    time_since_purchase = datetime.now() - tracking_data['buy_time']
                    hours, remainder = divmod(time_since_purchase.seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    time_str = f"{hours}h {minutes}m {seconds}s"
                    
                    # Calculate ROIs
                    high_price = tracking_data['high_price']
                    high_roi = ((high_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
                    
                    # Create a nice panel for price tracking info
                    roi_color = "green" if percent_change >= 0 else "red"
                    
                    tracking_panel = Panel.fit(
                        f"[bold cyan]Time since purchase:[/] {time_str}\n"
                        f"[bold cyan]Current price:[/] {current_price:.10f} SOL per token\n"
                        f"[bold cyan]Buy price:[/] {buy_price:.10f} SOL per token\n"
                        f"[bold cyan]Current ROI:[/] [bold {roi_color}]{percent_change:.2f}%[/]\n"
                        f"[bold cyan]High price:[/] {high_price:.10f} (ROI: {high_roi:.2f}%)\n"
                        f"[bold cyan]Low:[/] {tracking_data['low_price']:.10f}\n",
                        title=f"[bold blue]PAPER PRICE TRACKING - {token_address}[/]", 
                        border_style="blue"
                    )
                    console.print(tracking_panel)
                    
                    # Display trailing stop info if applicable
                    if exit_strategy == "TRAILING_STOP" and tracking_data['trailing_stop_price'] > 0:
                        trailing_stop_price = tracking_data['trailing_stop_price']
                        exit_roi = tracking_data.get('trailing_stop_roi', 0)
                        console.print(f"[cyan]TRAILING STOP:[/] {trailing_stop_price:.10f} (Exit ROI: {exit_roi:.2f}%)")
                        console.print(f"[cyan]Trail is {trailing_stop} percentage points below high ROI[/]")
                    
                    # Display auto-sell status
                    if PaperConfig.PAPER_AUTO_SELL:
                        console.print(f"[cyan]Paper auto-sell status:[/] {reason}")
                    
                    console.print("-" * 30)
                    
                    # Update last display time
                    last_display_time = current_time
            else:
                # Increment failure counter
                price_check_failures += 1
                
                # Only log failures after several attempts
                if price_check_failures > 5:
                    logger.warning(f"Failed to get paper price for {token_address} (failure #{price_check_failures})")
                
                # Add longer delay after repeated failures to avoid API hammering
                if price_check_failures > 10:
                    await asyncio.sleep(15)  # Back off for 15 seconds after 10 failures
                
        except Exception as e:
            # Only log failures after several attempts to reduce noise
            price_check_failures += 1
            if price_check_failures > 5:
                logger.error(f"Error in paper price tracking: {e}")
        
        # Wait for next check
        await asyncio.sleep(Config.PRICE_CHECK_INTERVAL + Config.RATE_LIMIT_DELAY)
    
    logger.info(f"Completed paper price tracking for {token_address}")

    # ================ INITIALIZATION ================
# Fixed version of initialize_paper_trading to skip measuring RPC delay on startup
def initialize_paper_trading():
    """Initialize paper trading module"""
    # Load settings from .env
    PaperConfig.load_from_env()
    
    # Don't measure RPC delay on startup to avoid event loop issues
    # measure_rpc_delay_sync()  # <- This line removed to prevent errors
    
    # Log initialization
    logger.info("Paper trading module initialized")
    logger.info(f"Paper trading enabled: {PaperConfig.PAPER_ENABLED}")
    logger.info(f"Paper SOL balance: {PaperConfig.PAPER_SOL_BALANCE} SOL")
    logger.info(f"Paper buy amount: {PaperConfig.PAPER_BUY_AMOUNT} SOL")
    
    return True

# ================ MAIN FUNCTION ================
if __name__ == "__main__":
    # Test paper trading functions
    print("Paper Trading Module - Test Mode")
    initialize_paper_trading()
    print(f"Paper SOL Balance: {PaperConfig.paper_portfolio['sol_balance']} SOL")
    print(f"Measured RPC Delay: {PaperConfig.avg_measured_delay:.2f} ms")#!/usr/bin/env python3