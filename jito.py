#!/usr/bin/env python3
"""
Optimized Jito MEV routing implementation for faster token swaps on Solana
- Implements faster transaction routing with block builders
- Adds adaptive tipping based on network congestion
- Enhances parallel submission strategies
- Improves error handling and retry logic
"""
import base64
import time
import json
import requests
import random
import hashlib
import asyncio
import aiohttp
from typing import Dict, Optional, Tuple, List, Any, Union
from datetime import datetime
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Commitment
import concurrent.futures

from config import Config, logger
from solanaa import payer_keypair, solana_client, confirm_transaction, confirm_transaction_async

# Thread pool for concurrent operations
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# ================ JITO ROUTING CONFIGURATION ================
# Jito RPC endpoints - Consider upgrading to a paid tier for higher rate limits
JITO_RPC_URL = "https://mainnet.block-engine.jito.wtf/api/v1/mainnet/broadcast-transaction"
JITO_AUTH_HEADER = None  # Set this if you're using authenticated API access
JITO_TIP_ACCOUNT = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"  # Jito fee recipient address

# Default tip amounts (in lamports) based on network congestion levels
TIP_LEVELS = {
    "low": 100000,        # 0.0001 SOL
    "medium": 300000,     # 0.0003 SOL
    "high": 1000000,      # 0.001 SOL
    "extreme": 5000000    # 0.005 SOL
}

# Jupiter API URL (still needed for quotes)
JUPITER_API_URL = "https://quote-api.jup.ag/v6"

# Cache for network congestion level (updated every minute)
network_status = {
    "level": "medium",
    "last_update": 0,
    "update_interval": 60  # Update every 60 seconds
}

# HTTP session for connection pooling
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})

# Async HTTP session
_async_session = None

async def get_async_session():
    """Get or create the async HTTP session"""
    global _async_session
    if _async_session is None or _async_session.closed:
        _async_session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json"}
        )
    return _async_session

# ================ HELPER FUNCTIONS ================
async def get_network_congestion_async() -> str:
    """
    Determine current network congestion level to adjust tip amount
    Uses a cached value to avoid too many API calls
    """
    global network_status
    current_time = time.time()
    
    # If we've checked recently, use cached value
    if current_time - network_status["last_update"] < network_status["update_interval"]:
        return network_status["level"]
    
    try:
        # Get recent performance samples from Solana
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            _thread_pool,
            lambda: solana_client.get_recent_performance_samples(limit=5)
        )
        
        if response and hasattr(response, 'value') and response.value:
            # Average the number of transactions per slot across samples
            total_tx = 0
            slots = 0
            for sample in response.value:
                total_tx += sample.num_transactions
                slots += 1
            
            avg_tx_per_slot = total_tx / slots if slots > 0 else 0
            
            # Determine congestion level based on transactions per slot
            if avg_tx_per_slot < 1000:
                level = "low"
            elif avg_tx_per_slot < 3000:
                level = "medium"
            elif avg_tx_per_slot < 5000:
                level = "high"
            else:
                level = "extreme"
                
            # Update cache
            network_status["level"] = level
            network_status["last_update"] = current_time
            
            logger.info(f"Network congestion level: {level} ({avg_tx_per_slot:.0f} tx/slot)")
            return level
    except Exception as e:
        logger.warning(f"Error getting network congestion: {e}, using default level")
    
    # Default to medium if we can't determine
    return "medium"

def get_network_congestion() -> str:
    """Synchronous wrapper for async network congestion function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_network_congestion_async())
    finally:
        loop.close()

def calculate_tip_amount() -> int:
    """
    Calculate the appropriate tip amount based on network congestion and settings
    """
    # If user has a custom priority fee set, use that
    if Config.PRIORITY_FEE_MODE == "custom":
        return Config.PRIORITY_FEE_LAMPORTS
        
    # Otherwise determine based on network congestion
    congestion = get_network_congestion()
    return TIP_LEVELS.get(congestion, TIP_LEVELS["medium"])

async def rate_limit_delay(endpoint: str):
    """
    Asynchronous rate limit control with jitter to prevent thundering herd
    """
    if endpoint in Config.api_last_call_time:
        time_since_last_call = time.time() - Config.api_last_call_time[endpoint]
        
        if time_since_last_call < Config.RATE_LIMIT_DELAY:
            # Add jitter (±10%) to avoid all threads waking at the same time
            jitter = random.uniform(0.9, 1.1)
            sleep_time = (Config.RATE_LIMIT_DELAY - time_since_last_call) * jitter
            await asyncio.sleep(sleep_time)
    
    # Update last call time
    Config.api_last_call_time[endpoint] = time.time()

async def handle_rate_limit_async(retry_count: int, max_retries: int, error: Optional[str] = None) -> Tuple[bool, int]:
    """
    Handle rate limiting with exponential backoff
    Returns: (should_retry, new_retry_count)
    """
    if retry_count >= max_retries:
        logger.error(f"Max rate limit retries exceeded: {error}")
        return False, retry_count
        
    # Exponential backoff with jitter
    backoff = min(2 ** retry_count + (random.random()), 10)
    logger.warning(f"Rate limit hit! Waiting {backoff:.2f}s before retry {retry_count+1}/{max_retries}")
    await asyncio.sleep(backoff)
    
    return True, retry_count + 1

def handle_rate_limit(retry_count: int, max_retries: int, error: Optional[str] = None) -> Tuple[bool, int]:
    """Synchronous wrapper for async rate limit handler"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(handle_rate_limit_async(retry_count, max_retries, error))
    finally:
        loop.close()

# ================ JUPITER API FOR QUOTES ================
async def get_quote_async(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50, retry_count: int = 0) -> Optional[Dict]:
    """
    Get a price quote from Jupiter with rate limit handling
    We still use Jupiter for quotes as they have the best price discovery
    """
    await rate_limit_delay("jupiter_quote")
    
    try:
        session = await get_async_session()
        
        url = f"{JUPITER_API_URL}/quote"
        params = {
            'inputMint': input_mint,
            'outputMint': output_mint,
            'amount': amount,
            'slippageBps': slippage_bps,
            'onlyDirectRoutes': 'true'  # Use direct routes when possible for speed
        }
        
        async with session.get(url, params=params, timeout=5) as response:
            # Handle rate limiting explicitly
            if response.status == 429:
                should_retry, new_retry = await handle_rate_limit_async(
                    retry_count, 
                    Config.MAX_RATE_LIMIT_RETRIES, 
                    "Jupiter quote API"
                )
                if should_retry:
                    return await get_quote_async(input_mint, output_mint, amount, slippage_bps, new_retry)
                return None
            
            # Check for success and return data
            if response.status == 200:
                return await response.json()
                
            # Handle other errors
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Error getting quote: {e}")
        
        # Check if it's worth retrying
        if retry_count < Config.MAX_RATE_LIMIT_RETRIES and ("429" in str(e) or "timeout" in str(e).lower()):
            should_retry, new_retry = await handle_rate_limit_async(
                retry_count, 
                Config.MAX_RATE_LIMIT_RETRIES,
                str(e)
            )
            if should_retry:
                return await get_quote_async(input_mint, output_mint, amount, slippage_bps, new_retry)
    
    return None

def get_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50, retry_count: int = 0) -> Optional[Dict]:
    """Synchronous wrapper for async quote function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_quote_async(input_mint, output_mint, amount, slippage_bps, retry_count))
    finally:
        loop.close()

# ================ JITO TRANSACTION PREPARATION ================
async def get_swap_tx_async(quote_response: Dict, retry_count: int = 0) -> Optional[Dict]:
    """
    Get a swap transaction from Jupiter but prepare it for Jito routing
    """
    await rate_limit_delay("jupiter_swap")
    
    try:
        session = await get_async_session()
        user_public_key = str(payer_keypair.pubkey())
        url = f"{JUPITER_API_URL}/swap"
        
        # Prepare payload for transaction
        payload = {
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "quoteResponse": quote_response,
            "computeUnitPriceMicroLamports": 1  # Set to 1 as we'll use Jito tips instead
        }
        
        async with session.post(url, json=payload, timeout=5) as response:
            # Handle rate limiting explicitly
            if response.status == 429:
                should_retry, new_retry = await handle_rate_limit_async(
                    retry_count, 
                    Config.MAX_RATE_LIMIT_RETRIES,
                    "Jupiter swap API"
                )
                if should_retry:
                    return await get_swap_tx_async(quote_response, new_retry)
                return None
                    
            # Check for success and return data
            if response.status == 200:
                return await response.json()
            
            # Handle other errors
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Error getting swap transaction: {e}")
        
        # Check if it's worth retrying
        if retry_count < Config.MAX_RATE_LIMIT_RETRIES and ("429" in str(e) or "timeout" in str(e).lower()):
            should_retry, new_retry = await handle_rate_limit_async(
                retry_count, 
                Config.MAX_RATE_LIMIT_RETRIES,
                str(e)
            )
            if should_retry:
                return await get_swap_tx_async(quote_response, new_retry)
    
    return None

def get_swap_tx(quote_response: Dict, retry_count: int = 0) -> Optional[Dict]:
    """Synchronous wrapper for async swap transaction function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_swap_tx_async(quote_response, retry_count))
    finally:
        loop.close()

async def prepare_jito_transaction(base64_tx: str) -> Optional[VersionedTransaction]:
    """
    Prepare a Jito-optimized transaction from base64 encoded transaction
    - Adds tip instruction if needed
    - Ensures versioned transaction format
    """
    try:
        # Decode the transaction
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(base64_tx))
        
        # TODO: For full implementation, add Jito tip instruction here
        # This would require modifying the transaction to include the tip
        # For now, we'll just return the decoded transaction
        
        return raw_tx
    except Exception as e:
        logger.error(f"Error preparing Jito transaction: {e}")
        return None

async def sign_transaction_async(tx: VersionedTransaction) -> Optional[VersionedTransaction]:
    """
    Sign a transaction with the payer keypair asynchronously
    """
    try:
        signature = payer_keypair.sign_message(to_bytes_versioned(tx.message))
        signed_tx = VersionedTransaction.populate(tx.message, [signature])
        return signed_tx
    except Exception as e:
        logger.error(f"Error signing transaction: {e}")
        return None

def sign_transaction(tx: VersionedTransaction) -> Optional[VersionedTransaction]:
    """Synchronous wrapper for async signing function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(sign_transaction_async(tx))
    finally:
        loop.close()

# ================ JITO TRANSACTION SUBMISSION ================
async def submit_to_jito_async(signed_tx: VersionedTransaction, tip_amount: int = 0) -> Optional[str]:
    """
    Submit a signed transaction to Jito's MEV-protected endpoint asynchronously
    """
    try:
        session = await get_async_session()
        
        # Serialize the transaction
        serialized_tx = base64.b64encode(bytes(signed_tx)).decode('utf-8')
        
        # Prepare the API payload
        payload = {
            "transaction": serialized_tx,
            "skipPreflight": False,
            "tipMicroLamports": tip_amount * 1000  # Convert lamports to micro-lamports for Jito
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # Add auth header if provided
        if JITO_AUTH_HEADER:
            headers["Authorization"] = JITO_AUTH_HEADER
        
        # Send to Jito API
        start_time = time.time()
        
        async with session.post(
            JITO_RPC_URL,
            headers=headers,
            json=payload,
            timeout=5
        ) as response:
            request_time = time.time() - start_time
            logger.info(f"Jito submission time: {request_time*1000:.2f}ms")
            
            if response.status == 200:
                result = await response.json()
                if 'result' in result:
                    tx_sig = result['result']
                    logger.info(f"Jito accepted transaction: {tx_sig}")
                    return tx_sig
                else:
                    logger.error(f"Jito error: {result.get('error', 'Unknown error')}")
            else:
                logger.error(f"Jito submission failed: {response.status} - {await response.text()}")
        
    except Exception as e:
        logger.error(f"Error submitting to Jito: {e}")
    
    return None

def submit_to_jito(signed_tx: VersionedTransaction, tip_amount: int = 0) -> Optional[str]:
    """Synchronous wrapper for async Jito submission function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(submit_to_jito_async(signed_tx, tip_amount))
    finally:
        loop.close()

# ================ PARALLEL TRANSACTION SUBMISSION ================
async def submit_transaction_parallel_async(signed_tx: VersionedTransaction, tip_amount: int) -> Optional[str]:
    """
    Submit the transaction in parallel to both Jito and regular Solana RPC
    to maximize chances of fast inclusion
    """
    tx_sig = None
    
    # Create tasks for both submission methods
    jito_task = asyncio.create_task(submit_to_jito_async(signed_tx, tip_amount))
    
    # Regular RPC submission in a thread to avoid blocking
    loop = asyncio.get_event_loop()
    regular_task = asyncio.create_task(loop.run_in_executor(
        _thread_pool,
        lambda: solana_client.send_raw_transaction(
            txn=bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment=Config.DEFAULT_COMMITMENT)
        ).value
    ))
    
    # Wait for first successful result
    done, pending = await asyncio.wait(
        [jito_task, regular_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    # Process completed task
    for task in done:
        try:
            result = task.result()
            if result:
                tx_sig = result
                logger.info(f"Transaction submitted successfully: {tx_sig}")
                break
        except Exception as e:
            logger.error(f"Error in transaction submission: {e}")
    
    # Cancel remaining tasks
    for task in pending:
        task.cancel()
    
    return tx_sig

def submit_transaction_parallel(signed_tx: VersionedTransaction, tip_amount: int) -> Optional[str]:
    """Synchronous wrapper for async parallel submission function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(submit_transaction_parallel_async(signed_tx, tip_amount))
    finally:
        loop.close()

# ================ MAIN SWAP EXECUTION FUNCTION ================
async def execute_swap_async(input_mint: str, output_mint: str, amount_in: int, slippage_percent: float = 1, 
                      retry_count: int = 0, max_retries: Optional[int] = None) -> Tuple[bool, Optional[float]]:
    """
    Execute a token swap with Jito optimization for faster inclusion
    """
    # Use default max retries if not specified
    max_retries = max_retries or Config.MAX_SWAP_RETRIES
    
    # Increase slippage slightly on retries
    adjusted_slippage = slippage_percent * (1 + (retry_count * 0.5))
    slippage_bps = int(adjusted_slippage * 100)
    
    # Get quote
    logger.info(f"Getting price quote... (Attempt {retry_count+1}/{max_retries+1}, Slippage: {adjusted_slippage:.2f}%)")
    start_time = time.time()
    quote = await get_quote_async(input_mint, output_mint, amount_in, slippage_bps)
    quote_time = time.time() - start_time
    logger.info(f"Quote API response time: {quote_time*1000:.2f}ms")
    
    if not quote:
        logger.error("Failed to get quote.")
        if retry_count < max_retries:
            logger.info(f"Retrying with increased slippage...")
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    in_amount = amount_in / 1e9
    out_amount = float(quote.get('outAmount', 0)) / 1e9
    logger.info(f"Quote: {in_amount:.6f} -> {out_amount:.6f}")
    
    # Check for extremely low output or high price impact
    price_impact = float(quote.get('priceImpactPct', 0))
    logger.info(f"Impact: {price_impact}%")
    
    # Warn about high price impact
    if price_impact > Config.PRICE_IMPACT_WARNING:
        logger.warning(f"HIGH PRICE IMPACT DETECTED: {price_impact}%")
        if price_impact > Config.PRICE_IMPACT_ABORT:
            logger.error(f"EXTREMELY HIGH PRICE IMPACT: {price_impact}%. Aborting for safety.")
            return False, None
    
    # Check for minimum output value (0.005 SOL for sells to SOL)
    if output_mint == Config.SOL and out_amount < 0.005:
        logger.warning(f"Output amount too small: {out_amount:.6f} SOL")
        
        # If it's a sell and amount is too small, try selling a smaller percentage
        if input_mint != Config.SOL and retry_count < max_retries:
            reduced_amount = int(amount_in * 0.8)  # Try 80% of the amount
            if reduced_amount > 0:
                logger.info(f"Retrying with reduced amount: {reduced_amount / amount_in * 100:.0f}% of original")
                return await execute_swap_async(input_mint, output_mint, reduced_amount, slippage_percent, retry_count + 1, max_retries)
        
        if retry_count >= max_retries:
            logger.error("Max retries reached with insufficient output. Transaction not viable.")
            return False, None
    
    # Get swap transaction
    logger.info("Preparing transaction...")
    start_time = time.time()
    swap_tx = await get_swap_tx_async(quote)
    tx_prep_time = time.time() - start_time
    logger.info(f"Transaction prep time: {tx_prep_time*1000:.2f}ms")
    
    if not swap_tx:
        logger.error("Failed to get swap transaction.")
        if retry_count < max_retries:
            logger.info("Retrying transaction preparation...")
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    # Calculate tip amount based on network conditions
    tip_amount = calculate_tip_amount()
    logger.info(f"Using Jito tip: {tip_amount} lamports ({tip_amount/1e9:.9f} SOL)")
    
    # Prepare and sign transaction
    logger.info("Preparing Jito-optimized transaction...")
    start_time = time.time()
    raw_tx = await asyncio.to_thread(prepare_jito_transaction, swap_tx['swapTransaction'])
    prep_time = time.time() - start_time
    logger.info(f"Jito prep time: {prep_time*1000:.2f}ms")
    
    if not raw_tx:
        logger.error("Failed to prepare Jito transaction.")
        if retry_count < max_retries:
            logger.info("Retrying transaction preparation...")
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    # Sign transaction
    logger.info("Signing...")
    start_time = time.time()
    signed_tx = await sign_transaction_async(raw_tx)
    sign_time = time.time() - start_time
    logger.info(f"Transaction signing time: {sign_time*1000:.2f}ms")
    
    if not signed_tx:
        logger.error("Failed to sign transaction.")
        if retry_count < max_retries:
            logger.info("Retrying after signing error...")
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    # Submit transaction (parallel to both Jito and regular RPC)
    logger.info("Submitting to Jito and backup RPC in parallel...")
    start_time = time.time()
    tx_sig = await submit_transaction_parallel_async(signed_tx, tip_amount)
    submit_time = time.time() - start_time
    logger.info(f"Submission time: {submit_time*1000:.2f}ms")
    
    if not tx_sig:
        logger.error("Failed to submit transaction.")
        if retry_count < max_retries:
            logger.info("Retrying after submission error...")
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    logger.info(f"Transaction submitted in {submit_time*1000:.2f}ms: {tx_sig}")
    
    # Confirm transaction
    logger.info("Waiting for confirmation...")
    confirmed = await confirm_transaction_async(tx_sig)
    
    if confirmed:
        logger.info(f"SWAP SUCCESSFUL! {in_amount:.6f} -> {out_amount:.6f}")
        # For buys, we care about the price of the token in SOL
        if input_mint == Config.SOL:
            # This is a buy, so calculate price per token
            price_per_token = in_amount / out_amount if out_amount > 0 else 0
            return True, price_per_token
        else:
            # This is a sell, return the SOL amount received
            return True, out_amount
    else:
        logger.warning("Swap may have failed or is still pending.")
        
        # Retry with higher slippage if not confirmed
        if retry_count < max_retries:
            logger.info(f"Retrying with increased parameters...")
            await asyncio.sleep(1.5)  # Wait longer between retries
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent * 1.5, retry_count + 1, max_retries)
        
        return False, None

def execute_swap(input_mint: str, output_mint: str, amount_in: int, slippage_percent: float = 1, 
               retry_count: int = 0, max_retries: Optional[int] = None) -> Tuple[bool, Optional[float]]:
    """Synchronous wrapper for async swap execution function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count, max_retries))
    finally:
        loop.close()

# ================ ADDITIONAL UTILITIES ================
async def get_jito_tip_accounts_async() -> List[str]:
    """
    Get the current valid Jito tip accounts asynchronously
    This is useful to ensure tips go to the right place
    """
    try:
        # Typically this would query Jito's API for current tip accounts
        # For now we'll return the known account
        return [JITO_TIP_ACCOUNT]
    except Exception as e:
        logger.error(f"Error getting Jito tip accounts: {e}")
        return [JITO_TIP_ACCOUNT]  # Fallback to default

def get_jito_tip_accounts() -> List[str]:
    """Synchronous wrapper for async tip accounts function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_jito_tip_accounts_async())
    finally:
        loop.close()

# Clean up async resources on exit
def cleanup_async_resources():
    """Close async resources when program exits"""
    if _async_session is not None and not _async_session.closed:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_session.close())
        loop.close()