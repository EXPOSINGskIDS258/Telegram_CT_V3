#!/usr/bin/env python3
"""
Raydium V4 Direct Swap Integration for Ultra-Fast Trading
- Direct AMM swaps without routing overhead
- Concentrated Liquidity Market Maker (CLMM) support
- Optimized for speed with minimal hops
"""
import asyncio
import base64
import struct
import time
import aiohttp
from typing import Dict, Optional, Tuple, List, Any, Callable
from datetime import datetime
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment
import requests
import logging

# Setup logger
logger = logging.getLogger(__name__)

# ================ RAYDIUM V4 CONSTANTS ================
RAYDIUM_V4_PROGRAM_ID = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")

# Token Program IDs
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# Default SOL mint
SOL_MINT = "So11111111111111111111111111111111111111112"

# Pool cache for faster lookups
pool_cache = {}
_session = None

# Raydium API endpoints
RAYDIUM_API_BASE = "https://api-v3.raydium.io"
RAYDIUM_POOL_API = f"{RAYDIUM_API_BASE}/pools/info/mint"
RAYDIUM_PRICE_API = f"{RAYDIUM_API_BASE}/mint/price"

# Global variables to be set by the main app
solana_client = None
payer_keypair = None
config = None
jito_submit_fn = None  # Function to submit via Jito

def initialize_raydium(client: Client, keypair: Keypair, app_config: Any, jito_submit: Optional[Callable] = None):
    """Initialize Raydium module with app dependencies"""
    global solana_client, payer_keypair, config, jito_submit_fn
    solana_client = client
    payer_keypair = keypair
    config = app_config
    jito_submit_fn = jito_submit
    logger.info("Raydium V4 module initialized")

# ================ HELPER FUNCTIONS ================
async def get_async_session():
    """Get or create async HTTP session"""
    global _session
    if _session is None:
        _session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10)
        )
    return _session

async def find_raydium_pool(token_a: str, token_b: str) -> Optional[Dict]:
    """Find Raydium V4 CLMM pool for token pair"""
    cache_key = f"{token_a}_{token_b}"
    
    # Check cache first
    if cache_key in pool_cache:
        return pool_cache[cache_key]
    
    try:
        session = await get_async_session()
        
        # Query pool by mints
        params = {
            "mint1": token_a,
            "mint2": token_b,
            "poolType": "concentrate",  # V4 CLMM pools
            "poolSortField": "liquidity",
            "sortType": "desc",
            "pageSize": 10
        }
        
        async with session.get(f"{RAYDIUM_API_BASE}/pools/info/list", params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("success") and data.get("data"):
                    pools = data["data"]["data"]
                    if pools:
                        # Get the pool with highest liquidity
                        best_pool = pools[0]
                        pool_cache[cache_key] = best_pool
                        logger.info(f"Found Raydium V4 pool: {best_pool['id']}")
                        return best_pool
        
        # Try reverse order
        cache_key_reverse = f"{token_b}_{token_a}"
        params["mint1"] = token_b
        params["mint2"] = token_a
        
        async with session.get(f"{RAYDIUM_API_BASE}/pools/info/list", params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("success") and data.get("data"):
                    pools = data["data"]["data"]
                    if pools:
                        best_pool = pools[0]
                        pool_cache[cache_key] = best_pool
                        pool_cache[cache_key_reverse] = best_pool
                        logger.info(f"Found Raydium V4 pool (reversed): {best_pool['id']}")
                        return best_pool
                        
    except Exception as e:
        logger.error(f"Error finding Raydium pool: {e}")
    
    return None

async def get_pool_keys(pool_id: str) -> Optional[Dict]:
    """Get detailed pool keys for a Raydium V4 pool"""
    try:
        session = await get_async_session()
        
        async with session.get(f"{RAYDIUM_API_BASE}/pools/key/ids", 
                             params={"ids": pool_id}) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("success") and data.get("data"):
                    return data["data"][0]
    except Exception as e:
        logger.error(f"Error getting pool keys: {e}")
    
    return None

async def calculate_swap_amounts(pool: Dict, amount_in: int, token_in: str) -> Tuple[int, float]:
    """Calculate output amount for Raydium V4 swap"""
    try:
        # Get current pool state
        pool_id = pool["id"]
        pool_keys = await get_pool_keys(pool_id)
        
        if not pool_keys:
            return 0, 0.0
        
        # Determine if we're swapping mint A to B or B to A
        mint_a = pool_keys["mintA"]["address"]
        mint_b = pool_keys["mintB"]["address"]
        
        is_base_to_quote = token_in == mint_a
        
        # Get current price and liquidity
        current_price = float(pool.get("price", 0))
        liquidity = float(pool.get("tvl", 0))
        
        if not current_price or not liquidity:
            return 0, 0.0
        
        # Simple constant product formula (x * y = k)
        # This is simplified - real CLMM math is more complex
        if is_base_to_quote:
            # Selling token A for token B
            price_impact = (amount_in / 1e9) / (liquidity / 2)  # Rough estimate
            effective_price = current_price * (1 - price_impact)
            amount_out = int(amount_in * effective_price)
        else:
            # Selling token B for token A
            price_impact = (amount_in / 1e9) / (liquidity / 2)
            effective_price = (1 / current_price) * (1 - price_impact)
            amount_out = int(amount_in * effective_price)
        
        # Apply fee (Raydium V4 typically 0.25%)
        fee_rate = 0.0025
        amount_out = int(amount_out * (1 - fee_rate))
        
        return amount_out, price_impact * 100
        
    except Exception as e:
        logger.error(f"Error calculating swap amounts: {e}")
        return 0, 0.0

async def build_swap_instruction(
    pool_keys: Dict,
    user_pubkey: Pubkey,
    amount_in: int,
    minimum_amount_out: int,
    token_in: str,
    user_token_in: Pubkey,
    user_token_out: Pubkey,
    compute_unit_price: int = 100000
) -> List[Instruction]:
    """Build Raydium V4 swap instruction"""
    instructions = []
    
    # Add compute budget instructions
    instructions.append(set_compute_unit_limit(400000))
    instructions.append(set_compute_unit_price(compute_unit_price))
    
    # Determine swap direction
    mint_a = pool_keys["mintA"]["address"]
    is_base_to_quote = token_in == mint_a
    
    # Build swap instruction data
    # Instruction discriminator for swap
    discriminator = bytes([0xf8, 0xc6, 0x9e, 0x91, 0xe1, 0x75, 0x87, 0xc8])
    
    # Pack instruction data
    data = discriminator
    data += struct.pack("<Q", amount_in)  # amount_in u64
    data += struct.pack("<Q", minimum_amount_out)  # minimum_amount_out u64
    data += struct.pack("<?", is_base_to_quote)  # is_base_to_quote bool
    
    # Build account metas
    pool_id = Pubkey.from_string(pool_keys["id"])
    amm_config = Pubkey.from_string(pool_keys["ammConfig"]["address"])
    pool_state = pool_id  # In V4, pool ID is the pool state
    
    accounts = [
        AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),  # payer
        AccountMeta(pubkey=amm_config, is_signer=False, is_writable=False),  # amm_config
        AccountMeta(pubkey=pool_state, is_signer=False, is_writable=True),  # pool_state
        AccountMeta(pubkey=user_token_in, is_signer=False, is_writable=True),  # user_token_in
        AccountMeta(pubkey=user_token_out, is_signer=False, is_writable=True),  # user_token_out
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["vault"]["A"]), is_signer=False, is_writable=True),  # vault_a
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["vault"]["B"]), is_signer=False, is_writable=True),  # vault_b
        AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # token_program
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["mintA"]["program"]), is_signer=False, is_writable=False),  # token_program_a
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["mintB"]["program"]), is_signer=False, is_writable=False),  # token_program_b
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["vault"]["A"]), is_signer=False, is_writable=False),  # tick_array_0
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["vault"]["B"]), is_signer=False, is_writable=False),  # tick_array_1
        AccountMeta(pubkey=Pubkey.from_string(pool_keys["vault"]["A"]), is_signer=False, is_writable=False),  # tick_array_2
        AccountMeta(pubkey=Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"), is_signer=False, is_writable=False),  # oracle
    ]
    
    swap_instruction = Instruction(
        program_id=RAYDIUM_V4_PROGRAM_ID,
        accounts=accounts,
        data=data
    )
    
    instructions.append(swap_instruction)
    
    return instructions

async def get_associated_token_address(mint: Pubkey, owner: Pubkey) -> Pubkey:
    """Derive associated token address"""
    return Pubkey.find_program_address(
        [
            bytes(owner),
            bytes(TOKEN_PROGRAM_ID),
            bytes(mint)
        ],
        ASSOCIATED_TOKEN_PROGRAM_ID
    )[0]

# ================ MAIN SWAP FUNCTION ================
async def execute_raydium_swap(
    input_mint: str,
    output_mint: str,
    amount_in: int,
    slippage_percent: float = 1.0,
    use_jito: bool = True,
    priority_fee: int = 100000,
    price_impact_warning: float = 5.0,
    price_impact_abort: float = 10.0,
    confirm_commitment: str = "confirmed"
) -> Tuple[bool, Optional[float]]:
    """Execute a direct Raydium V4 swap"""
    
    # Check if module is initialized
    if not solana_client or not payer_keypair:
        logger.error("Raydium module not initialized. Call initialize_raydium() first.")
        return False, None
    
    try:
        start_time = time.time()
        
        # Find pool
        logger.info("Finding Raydium V4 pool...")
        pool = await find_raydium_pool(input_mint, output_mint)
        
        if not pool:
            logger.error("No Raydium V4 pool found for this pair")
            return False, None
        
        pool_time = time.time() - start_time
        logger.info(f"Pool lookup time: {pool_time*1000:.2f}ms")
        
        # Get pool keys
        pool_keys = await get_pool_keys(pool["id"])
        if not pool_keys:
            logger.error("Failed to get pool keys")
            return False, None
        
        # Calculate swap amounts
        logger.info("Calculating swap amounts...")
        calc_start = time.time()
        amount_out, price_impact = await calculate_swap_amounts(pool, amount_in, input_mint)
        calc_time = time.time() - calc_start
        logger.info(f"Calculation time: {calc_time*1000:.2f}ms")
        
        if amount_out == 0:
            logger.error("Invalid output amount calculated")
            return False, None
        
        # Apply slippage
        minimum_amount_out = int(amount_out * (1 - slippage_percent / 100))
        
        in_amount = amount_in / 1e9
        out_amount = amount_out / 1e9
        logger.info(f"Swap: {in_amount:.6f} -> {out_amount:.6f}")
        logger.info(f"Price Impact: {price_impact:.2f}%")
        
        # Warn about high price impact
        if price_impact > price_impact_warning:
            logger.warning(f"HIGH PRICE IMPACT: {price_impact:.2f}%")
            if price_impact > price_impact_abort:
                logger.error("Price impact too high, aborting")
                return False, None
        
        # Get token accounts
        user_pubkey = payer_keypair.pubkey()
        input_mint_pubkey = Pubkey.from_string(input_mint)
        output_mint_pubkey = Pubkey.from_string(output_mint)
        
        # Get or create associated token accounts
        if input_mint == SOL_MINT:
            user_token_in = user_pubkey  # Native SOL account
        else:
            user_token_in = await get_associated_token_address(input_mint_pubkey, user_pubkey)
            
        if output_mint == SOL_MINT:
            user_token_out = user_pubkey  # Native SOL account
        else:
            user_token_out = await get_associated_token_address(output_mint_pubkey, user_pubkey)
        
        # Build transaction
        logger.info("Building swap transaction...")
        build_start = time.time()
        
        instructions = await build_swap_instruction(
            pool_keys,
            user_pubkey,
            amount_in,
            minimum_amount_out,
            input_mint,
            user_token_in,
            user_token_out,
            priority_fee
        )
        
        # Get recent blockhash
        recent_blockhash_resp = solana_client.get_latest_blockhash()
        recent_blockhash = recent_blockhash_resp.value.blockhash
        
        # Create message
        message = MessageV0.try_compile(
            payer=user_pubkey,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(str(recent_blockhash))
        )
        
        # Create and sign transaction
        transaction = VersionedTransaction(message, [payer_keypair])
        
        build_time = time.time() - build_start
        logger.info(f"Transaction build time: {build_time*1000:.2f}ms")
        
        # Submit transaction
        logger.info("Submitting transaction...")
        submit_start = time.time()
        
        if use_jito and jito_submit_fn:
            # Use Jito submission if available
            tx_sig = jito_submit_fn(transaction, priority_fee)
        else:
            # Direct submission
            response = solana_client.send_raw_transaction(
                txn=bytes(transaction),
                opts={"skip_preflight": True, "preflight_commitment": "processed"}
            )
            tx_sig = str(response.value) if response.value else None
        
        submit_time = time.time() - submit_start
        logger.info(f"Submission time: {submit_time*1000:.2f}ms")
        
        if not tx_sig:
            logger.error("Failed to submit transaction")
            return False, None
        
        logger.info(f"Transaction submitted: {tx_sig}")
        
        # Confirm transaction
        logger.info("Confirming transaction...")
        confirm_start = time.time()
        
        # Simple confirmation check
        confirmed = await confirm_transaction_raydium(tx_sig, commitment=confirm_commitment)
        
        confirm_time = time.time() - confirm_start
        total_time = time.time() - start_time
        
        if confirmed:
            logger.info(f"RAYDIUM SWAP SUCCESSFUL!")
            logger.info(f"Total time: {total_time*1000:.2f}ms")
            logger.info(f"- Pool lookup: {pool_time*1000:.2f}ms")
            logger.info(f"- Calculation: {calc_time*1000:.2f}ms") 
            logger.info(f"- Build: {build_time*1000:.2f}ms")
            logger.info(f"- Submit: {submit_time*1000:.2f}ms")
            logger.info(f"- Confirm: {confirm_time*1000:.2f}ms")
            
            # Calculate price per token for return value
            if input_mint == SOL_MINT:
                # Buy transaction - return price per token
                price_per_token = in_amount / out_amount if out_amount > 0 else 0
                return True, price_per_token
            else:
                # Sell transaction - return SOL received
                return True, out_amount
        else:
            logger.warning("Transaction may have failed or is still pending")
            return False, None
            
    except Exception as e:
        logger.error(f"Raydium swap error: {e}")
        return False, None

async def confirm_transaction_raydium(tx_sig: str, commitment: str = "confirmed", max_retries: int = 20) -> bool:
    """Confirm transaction with retries"""
    for i in range(max_retries):
        try:
            # Check transaction status
            response = solana_client.get_signature_statuses([tx_sig])
            if response and response.value and response.value[0]:
                status = response.value[0]
                if status.confirmation_status in ["confirmed", "finalized"]:
                    return True
                elif status.err:
                    logger.error(f"Transaction error: {status.err}")
                    return False
        except Exception as e:
            logger.debug(f"Confirmation check {i+1} failed: {e}")
        
        await asyncio.sleep(0.5)
    
    return False

# ================ PRICE CHECKING ================
async def get_raydium_price(token_mint: str) -> Optional[float]:
    """Get current token price from Raydium"""
    try:
        session = await get_async_session()
        
        # Get price in USDC
        async with session.get(f"{RAYDIUM_PRICE_API}?mints={token_mint}") as response:
            if response.status == 200:
                data = await response.json()
                if data.get("success") and data.get("data"):
                    price_data = data["data"].get(token_mint)
                    if price_data:
                        return float(price_data.get("price", 0))
    except Exception as e:
        logger.error(f"Error getting Raydium price: {e}")
    
    return None

async def get_quote_raydium(input_mint: str, output_mint: str, amount_in: int) -> Optional[Dict]:
    """Get a quote from Raydium (compatible with Jupiter format)"""
    try:
        # Find pool
        pool = await find_raydium_pool(input_mint, output_mint)
        if not pool:
            return None
        
        # Calculate amounts
        amount_out, price_impact = await calculate_swap_amounts(pool, amount_in, input_mint)
        
        if amount_out == 0:
            return None
        
        # Return Jupiter-compatible format
        return {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "inAmount": str(amount_in),
            "outAmount": str(amount_out),
            "priceImpactPct": price_impact,
            "inputDecimals": 9,
            "outputDecimals": 9,
            "route": [{
                "percent": 100,
                "marketInfos": [{
                    "id": pool["id"],
                    "label": "Raydium V4 CLMM",
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "inAmount": str(amount_in),
                    "outAmount": str(amount_out)
                }]
            }]
        }
        
    except Exception as e:
        logger.error(f"Error getting Raydium quote: {e}")
        return None

# ================ INTEGRATION FUNCTIONS ================
def execute_swap(input_mint: str, output_mint: str, amount_in: int, 
                slippage_percent: float = 1, retry_count: int = 0, 
                max_retries: Optional[int] = None) -> Tuple[bool, Optional[float]]:
    """Drop-in replacement for Jupiter execute_swap using Raydium"""
    # Get config values if available
    if config:
        priority_fee = getattr(config, 'PRIORITY_FEE_LAMPORTS', 100000)
        price_impact_warning = getattr(config, 'PRICE_IMPACT_WARNING', 5.0)
        price_impact_abort = getattr(config, 'PRICE_IMPACT_ABORT', 10.0)
        sol_mint = getattr(config, 'SOL', SOL_MINT)
    else:
        priority_fee = 100000
        price_impact_warning = 5.0
        price_impact_abort = 10.0
        sol_mint = SOL_MINT
    
    # Run async function
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            execute_raydium_swap(
                input_mint, 
                output_mint, 
                amount_in, 
                slippage_percent, 
                use_jito=True,
                priority_fee=priority_fee,
                price_impact_warning=price_impact_warning,
                price_impact_abort=price_impact_abort
            )
        )
    finally:
        loop.close()

def get_quote(input_mint: str, output_mint: str, amount_in: int, 
              slippage_bps: int = 50, retry_count: int = 0) -> Optional[Dict]:
    """Drop-in replacement for Jupiter get_quote using Raydium"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            get_quote_raydium(input_mint, output_mint, amount_in)
        )
    finally:
        loop.close()

# Async versions for direct use
execute_swap_async = execute_raydium_swap
get_quote_async = get_quote_raydium

# ================ CLEANUP ================
async def cleanup():
    """Clean up resources"""
    global _session
    if _session:
        await _session.close()
        _session = None

# Register cleanup
import atexit
atexit.register(lambda: asyncio.run(cleanup()))

# ================ EXAMPLE USAGE ================
if __name__ == "__main__":
    # Test finding a pool
    async def test():
        # SOL-USDC pool
        pool = await find_raydium_pool(
            SOL_MINT,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
        )
        print(f"Found pool: {pool}")
        
        # Test quote
        quote = await get_quote_raydium(
            SOL_MINT,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            int(0.1 * 1e9)  # 0.1 SOL
        )
        print(f"Quote: {quote}")
    
    asyncio.run(test())