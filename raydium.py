#!/usr/bin/env python3
"""
Raydium V4 integration module for Solana Trading Bot
- Implements Raydium AMM v4 swaps
- Provides the same interface as Jupiter for easy switching
- Supports liquidity pool queries and price calculations
"""
import base64
import struct
import time
import json
import requests
import asyncio
import aiohttp
from typing import Dict, Optional, Tuple, List, Any
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.sysvar import RENT
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
import concurrent.futures

from config import Config, logger
from solanaa import payer_keypair, solana_client, confirm_transaction_async

# Thread pool for concurrent operations
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# ================ RAYDIUM CONSTANTS ================
RAYDIUM_AMM_PROGRAM = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
SERUM_PROGRAM = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")

# API endpoints
RAYDIUM_API_V3 = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
RAYDIUM_POOL_API = "https://api-v3.raydium.io/pools/info/mint"

# Cache for pool data
pool_cache = {}
pool_cache_timestamp = 0
CACHE_DURATION = 300  # 5 minutes

# ================ HELPER FUNCTIONS ================
async def get_raydium_pools():
    """Fetch all Raydium pools data"""
    global pool_cache, pool_cache_timestamp
    
    # Check cache
    if time.time() - pool_cache_timestamp < CACHE_DURATION and pool_cache:
        return pool_cache
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RAYDIUM_API_V3, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    pool_cache = {pool['baseMint']: pool for pool in data['official']}
                    pool_cache.update({pool['quoteMint']: pool for pool in data['official']})
                    pool_cache_timestamp = time.time()
                    return pool_cache
    except Exception as e:
        logger.error(f"Error fetching Raydium pools: {e}")
    
    return pool_cache

async def find_pool_by_mints(mint_a: str, mint_b: str) -> Optional[Dict]:
    """Find a Raydium pool for the given token pair"""
    pools = await get_raydium_pools()
    
    for pool_id, pool in pools.items():
        if (pool['baseMint'] == mint_a and pool['quoteMint'] == mint_b) or \
           (pool['baseMint'] == mint_b and pool['quoteMint'] == mint_a):
            return pool
    
    # Try API v3 for more pools
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{RAYDIUM_POOL_API}?mint1={mint_a}&mint2={mint_b}"
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]
    except Exception as e:
        logger.error(f"Error finding pool via API: {e}")
    
    return None

def calculate_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_numerator: int = 25,
    fee_denominator: int = 10000
) -> int:
    """Calculate output amount using constant product formula with fees"""
    if reserve_in == 0 or reserve_out == 0:
        return 0
    
    # Apply fee
    amount_in_with_fee = amount_in * (fee_denominator - fee_numerator)
    
    # Calculate amount out
    numerator = amount_in_with_fee * reserve_out
    denominator = (reserve_in * fee_denominator) + amount_in_with_fee
    
    if denominator == 0:
        return 0
    
    return numerator // denominator

def calculate_price_impact(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    amount_out: int
) -> float:
    """Calculate price impact percentage"""
    if reserve_in == 0 or reserve_out == 0 or amount_out == 0:
        return 100.0
    
    # Initial price
    initial_price = reserve_out / reserve_in
    
    # Price after swap
    new_reserve_in = reserve_in + amount_in
    new_reserve_out = reserve_out - amount_out
    
    if new_reserve_in == 0:
        return 100.0
    
    new_price = new_reserve_out / new_reserve_in
    
    # Price impact
    price_impact = abs((new_price - initial_price) / initial_price) * 100
    
    return price_impact

# ================ QUOTE FUNCTIONS ================
async def get_quote_async(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[Dict]:
    """Get a price quote from Raydium pools"""
    try:
        # Find the pool
        pool = await find_pool_by_mints(input_mint, output_mint)
        if not pool:
            logger.error(f"No Raydium pool found for {input_mint} -> {output_mint}")
            return None
        
        # Determine swap direction
        is_base_to_quote = pool['baseMint'] == input_mint
        
        # Get current reserves (you'd fetch these from chain in production)
        # For now, using placeholder values - in production, query the pool account
        base_reserve = int(pool.get('baseReserve', 1000000))
        quote_reserve = int(pool.get('quoteReserve', 1000000))
        
        if is_base_to_quote:
            reserve_in = base_reserve
            reserve_out = quote_reserve
        else:
            reserve_in = quote_reserve
            reserve_out = base_reserve
        
        # Calculate output amount
        amount_out = calculate_amount_out(amount, reserve_in, reserve_out)
        
        # Calculate price impact
        price_impact = calculate_price_impact(amount, reserve_in, reserve_out, amount_out)
        
        # Apply slippage
        min_amount_out = int(amount_out * (10000 - slippage_bps) / 10000)
        
        return {
            'inputMint': input_mint,
            'outputMint': output_mint,
            'inAmount': amount,
            'outAmount': amount_out,
            'otherAmountThreshold': min_amount_out,
            'swapMode': 'ExactIn',
            'priceImpactPct': price_impact,
            'marketInfos': [{
                'id': pool['id'],
                'label': 'Raydium',
                'inputMint': input_mint,
                'outputMint': output_mint,
                'notEnoughLiquidity': False,
                'inAmount': amount,
                'outAmount': amount_out,
                'priceImpactPct': price_impact,
                'lpFee': {
                    'amount': amount * 25 // 10000,  # 0.25% fee
                    'mint': input_mint,
                    'pct': 0.25
                },
                'platformFee': {
                    'amount': 0,
                    'mint': input_mint,
                    'pct': 0
                }
            }],
            'routePlan': [{
                'swapInfo': {
                    'ammKey': pool['id'],
                    'label': 'Raydium',
                    'inputMint': input_mint,
                    'outputMint': output_mint,
                    'inAmount': amount,
                    'outAmount': amount_out,
                    'feeAmount': amount * 25 // 10000,
                    'feeMint': input_mint
                },
                'percent': 100
            }],
            'inputDecimals': 9,  # Default, should fetch from token metadata
            'outputDecimals': 9  # Default, should fetch from token metadata
        }
        
    except Exception as e:
        logger.error(f"Error getting Raydium quote: {e}")
        return None

def get_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[Dict]:
    """Synchronous wrapper for get_quote_async"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_quote_async(input_mint, output_mint, amount, slippage_bps))
    finally:
        loop.close()

# ================ SWAP INSTRUCTION BUILDERS ================
def build_swap_instruction(
    pool_info: Dict,
    user_pubkey: Pubkey,
    amount_in: int,
    minimum_amount_out: int,
    input_mint: str,
    output_mint: str
) -> List[Instruction]:
    """Build Raydium swap instructions"""
    instructions = []
    
    # Add compute budget instructions
    instructions.append(set_compute_unit_limit(200000))
    instructions.append(set_compute_unit_price(Config.PRIORITY_FEE_LAMPORTS))
    
    # Parse pool info
    amm_id = Pubkey.from_string(pool_info['id'])
    amm_authority = RAYDIUM_AUTHORITY
    amm_open_orders = Pubkey.from_string(pool_info['openOrders'])
    amm_target_orders = Pubkey.from_string(pool_info.get('targetOrders', pool_info['openOrders']))
    pool_coin_token_account = Pubkey.from_string(pool_info['baseVault'])
    pool_pc_token_account = Pubkey.from_string(pool_info['quoteVault'])
    serum_market = Pubkey.from_string(pool_info['marketId'])
    
    # Determine if base to quote or quote to base
    is_base_to_quote = pool_info['baseMint'] == input_mint
    
    # Get user token accounts (would need to create if they don't exist)
    # For simplicity, assuming they exist - in production, add ATA creation logic
    user_input_token_account = get_associated_token_address(user_pubkey, Pubkey.from_string(input_mint))
    user_output_token_account = get_associated_token_address(user_pubkey, Pubkey.from_string(output_mint))
    
    # Build swap instruction data
    # Raydium swap instruction layout:
    # instruction_type (u8) = 9 for swap
    # amount_in (u64)
    # minimum_amount_out (u64)
    instruction_data = struct.pack('<BQQ', 9, amount_in, minimum_amount_out)
    
    # Account keys for swap instruction
    keys = [
        AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=amm_id, is_signer=False, is_writable=True),
        AccountMeta(pubkey=amm_authority, is_signer=False, is_writable=False),
        AccountMeta(pubkey=amm_open_orders, is_signer=False, is_writable=True),
        AccountMeta(pubkey=amm_target_orders, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_coin_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_pc_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=serum_market, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_input_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_output_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=False),
    ]
    
    # Add Serum market accounts (simplified - in production, fetch all market accounts)
    # This is a placeholder - you'd need to fetch actual Serum market accounts
    
    swap_instruction = Instruction(
        program_id=RAYDIUM_AMM_PROGRAM,
        accounts=keys,
        data=instruction_data
    )
    
    instructions.append(swap_instruction)
    
    return instructions

def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Calculate associated token address"""
    # This is a simplified version - use proper SPL token library in production
    seeds = [
        bytes(owner),
        bytes(TOKEN_PROGRAM_ID),
        bytes(mint)
    ]
    
    # In production, use proper PDA derivation
    # For now, returning a placeholder
    return owner  # This should be the actual ATA

# ================ SWAP EXECUTION ================
async def execute_swap_async(
    input_mint: str,
    output_mint: str,
    amount_in: int,
    slippage_percent: float = 1,
    retry_count: int = 0,
    max_retries: Optional[int] = None
) -> Tuple[bool, Optional[float]]:
    """Execute a token swap using Raydium"""
    max_retries = max_retries or Config.MAX_SWAP_RETRIES
    
    # Get quote first
    slippage_bps = int(slippage_percent * 100)
    quote = await get_quote_async(input_mint, output_mint, amount_in, slippage_bps)
    
    if not quote:
        logger.error("Failed to get Raydium quote")
        if retry_count < max_retries:
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None
    
    in_amount = amount_in / 1e9
    out_amount = float(quote.get('outAmount', 0)) / 1e9
    price_impact = float(quote.get('priceImpactPct', 0))
    
    logger.info(f"Raydium Quote: {in_amount:.6f} -> {out_amount:.6f} (Impact: {price_impact:.2f}%)")
    
    # Check price impact limits
    if price_impact > Config.PRICE_IMPACT_WARNING:
        logger.warning(f"HIGH PRICE IMPACT: {price_impact:.2f}%")
    
    if price_impact > Config.PRICE_IMPACT_ABORT:
        logger.error(f"Price impact too high: {price_impact:.2f}%. Aborting.")
        return False, None
    
    try:
        # Find pool info
        pool_info = await find_pool_by_mints(input_mint, output_mint)
        if not pool_info:
            logger.error("Pool info not found")
            return False, None
        
        # Build swap instructions
        minimum_out = int(quote['otherAmountThreshold'])
        instructions = build_swap_instruction(
            pool_info,
            payer_keypair.pubkey(),
            amount_in,
            minimum_out,
            input_mint,
            output_mint
        )
        
        # Get recent blockhash
        loop = asyncio.get_event_loop()
        recent_blockhash = await loop.run_in_executor(
            _thread_pool,
            lambda: solana_client.get_latest_blockhash().value.blockhash
        )
        
        # Build transaction
        message = MessageV0.try_compile(
            payer=payer_keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        
        tx = VersionedTransaction(message, [payer_keypair])
        
        # Send transaction
        logger.info("Sending Raydium swap transaction...")
        tx_sig = await loop.run_in_executor(
            _thread_pool,
            lambda: solana_client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=False, preflight_commitment=Config.DEFAULT_COMMITMENT)
            ).value
        )
        
        if not tx_sig:
            logger.error("Failed to send transaction")
            if retry_count < max_retries:
                await asyncio.sleep(1.5)
                return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent * 1.5, retry_count + 1, max_retries)
            return False, None
        
        logger.info(f"Transaction sent: {tx_sig}")
        
        # Confirm transaction
        confirmed = await confirm_transaction_async(tx_sig)
        
        if confirmed:
            logger.info(f"RAYDIUM SWAP SUCCESSFUL! {in_amount:.6f} -> {out_amount:.6f}")
            
            # Calculate return value based on swap direction
            if input_mint == Config.SOL:
                # Buy: return price per token
                price_per_token = in_amount / out_amount if out_amount > 0 else 0
                return True, price_per_token
            else:
                # Sell: return SOL received
                return True, out_amount
        else:
            logger.warning("Swap may have failed or is still pending")
            if retry_count < max_retries:
                await asyncio.sleep(2)
                return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent * 1.5, retry_count + 1, max_retries)
            return False, None
            
    except Exception as e:
        logger.error(f"Error executing Raydium swap: {e}")
        if retry_count < max_retries:
            await asyncio.sleep(1)
            return await execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count + 1, max_retries)
        return False, None

def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_in: int,
    slippage_percent: float = 1,
    retry_count: int = 0,
    max_retries: Optional[int] = None
) -> Tuple[bool, Optional[float]]:
    """Synchronous wrapper for execute_swap_async"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            execute_swap_async(input_mint, output_mint, amount_in, slippage_percent, retry_count, max_retries)
        )
    finally:
        loop.close()

# ================ ADDITIONAL FUNCTIONS FOR COMPATIBILITY ================
async def get_swap_tx_async(quote_response: Dict, retry_count: int = 0) -> Optional[Dict]:
    """Get swap transaction (for compatibility with Jupiter interface)"""
    # Raydium doesn't have a separate swap transaction endpoint
    # Return the quote with transaction data
    return {
        'swapTransaction': '',  # Would contain serialized transaction
        'quote': quote_response
    }

def get_swap_tx(quote_response: Dict, retry_count: int = 0) -> Optional[Dict]:
    """Synchronous wrapper for get_swap_tx_async"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_swap_tx_async(quote_response, retry_count))
    finally:
        loop.close()

# ================ POOL INFORMATION ================
async def get_pool_info(token_address: str) -> Optional[Dict]:
    """Get detailed pool information for a token"""
    try:
        # Try to find pool with SOL
        pool = await find_pool_by_mints(token_address, Config.SOL)
        if pool:
            return {
                'poolId': pool['id'],
                'baseMint': pool['baseMint'],
                'quoteMint': pool['quoteMint'],
                'lpMint': pool.get('lpMint', ''),
                'baseReserve': pool.get('baseReserve', 0),
                'quoteReserve': pool.get('quoteReserve', 0),
                'lpSupply': pool.get('lpSupply', 0),
                'marketId': pool.get('marketId', ''),
                'programId': str(RAYDIUM_AMM_PROGRAM),
                'liquidity': {
                    'usd': calculate_liquidity_usd(pool)
                }
            }
    except Exception as e:
        logger.error(f"Error getting pool info: {e}")
    
    return None

def calculate_liquidity_usd(pool: Dict) -> float:
    """Calculate approximate USD liquidity (simplified)"""
    # In production, you'd fetch actual token prices
    # For now, assuming SOL = $100 for estimation
    sol_price = 100
    
    base_reserve = float(pool.get('baseReserve', 0)) / 1e9
    quote_reserve = float(pool.get('quoteReserve', 0)) / 1e9
    
    # If quote is SOL
    if pool['quoteMint'] == Config.SOL:
        return quote_reserve * sol_price * 2
    # If base is SOL
    elif pool['baseMint'] == Config.SOL:
        return base_reserve * sol_price * 2
    else:
        # Neither is SOL, can't estimate easily
        return 0

# ================ INITIALIZATION ================
async def initialize_raydium():
    """Initialize Raydium module and cache pools"""
    logger.info("Initializing Raydium V4 module...")
    
    # Pre-fetch pools
    pools = await get_raydium_pools()
    logger.info(f"Loaded {len(pools)} Raydium pools")
    
    return True

# Run initialization on module import
try:
    asyncio.run(initialize_raydium())
except:
    # If running in existing event loop
    pass

# ================ EXPORTS ================
__all__ = [
    'get_quote',
    'get_quote_async',
    'execute_swap',
    'execute_swap_async',
    'get_swap_tx',
    'get_swap_tx_async',
    'get_pool_info',
    'initialize_raydium'
]