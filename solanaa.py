#!/usr/bin/env python3
"""
Optimized Solana blockchain integration module for the trading bot
- Adds parallel RPC processing
- Implements faster transaction confirmation
- Reduces blocking operations
"""
import asyncio
import base64
import aiohttp
import time
from datetime import datetime
import requests
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.signature import Signature
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction
import os
import concurrent.futures
import random
from config import Config, logger

# Initialize clients with connection pooling
solana_client = None  # Will be initialized after getting RPC configuration
backup_client = None

# Global variables
payer_keypair = None

# Thread pool for parallel operations
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# ================ SOLANA UTILITIES ================
def initialize_clients(rpc_url=None):
    """Initialize Solana clients with provided RPC URL or from config with connection pooling"""
    global solana_client, backup_client
    
    if rpc_url:
        Config.RPC = rpc_url
    
    # Initialize with connection pooling options
    solana_client = Client(Config.RPC, timeout=5)
    backup_client = Client(Config.BACKUP_RPC, timeout=8)
    
    logger.info(f"Solana clients initialized with primary RPC: {Config.RPC}")
    
    return solana_client

def initialize_wallet(private_key=None, use_existing=False):
    """Initialize wallet with provided private key or generate new one with better error handling"""
    global payer_keypair
    
    # Try to use existing wallet session if requested
    if use_existing and os.path.exists("wallet.dat"):
        try:
            with open("wallet.dat", "rb") as f:
                seed_bytes = f.read()
                payer_keypair = Keypair.from_bytes(seed_bytes)
            logger.info(f"Loaded existing wallet: {payer_keypair.pubkey()}")
            return payer_keypair
        except Exception as e:
            logger.error(f"Error loading existing wallet: {e}")
            # Continue to create a new wallet instead of returning None
    
    # Initialize with provided private key
    if private_key:
        try:
            payer_keypair = Keypair.from_base58_string(private_key)
            logger.info(f"Wallet initialized: {payer_keypair.pubkey()}")
            
            # Save this keypair for future use
            try:
                with open("wallet.dat", "wb") as f:
                    f.write(bytes(payer_keypair))
            except Exception as e:
                logger.warning(f"Could not save wallet: {e}")
                
            return payer_keypair
        except Exception as e:
            logger.error(f"Error initializing wallet from private key: {e}")
            logger.info("Generating new keypair instead...")
    
    # Generate new keypair
    payer_keypair = Keypair()
    logger.info(f"Generated new wallet: {payer_keypair.pubkey()}")
    
    # Try to display the private key if available
    try:
        # Convert keypair bytes to base58 (this is the private key)
        private_key_base58 = base58_encode(bytes(payer_keypair))
        logger.info(f"Private key: {private_key_base58}")
        logger.warning("Make sure to save this private key if funding this wallet!")
    except Exception as e:
        logger.warning(f"Could not display private key: {e}")
    
    # Save this keypair for future use
    try:
        with open("wallet.dat", "wb") as f:
            f.write(bytes(payer_keypair))
    except Exception as e:
        logger.warning(f"Could not save wallet: {e}")
    
    return payer_keypair

def base58_encode(data):
    """Encode bytes to base58 string (for displaying private keys)"""
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    
    # Convert to integer
    num = int.from_bytes(data, byteorder='big')
    
    # Encode to base58
    encoded = ''
    base = len(alphabet)
    while num > 0:
        num, remainder = divmod(num, base)
        encoded = alphabet[remainder] + encoded
    
    # Add leading 1s for each leading zero byte
    for byte in data:
        if byte != 0:
            break
        encoded = alphabet[0] + encoded
    
    return encoded

async def get_token_balance_async(mint_str):
    """Asynchronous token balance retrieval with fallback support"""
    try:
        pubkey_str = str(payer_keypair.pubkey())
        headers = {"accept": "application/json", "content-type": "application/json"}
        payload = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "getTokenAccountsByOwner",
            "params": [
                pubkey_str,
                {"mint": mint_str},
                {"encoding": "jsonParsed"}
            ],
        }
        
        # Define the async function for making RPC requests
        async def try_rpc(url, timeout=3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
                        if response.status == 200:
                            return await response.json()
                return None
            except Exception:
                return None
        
        # Try both RPCs in parallel
        primary_task = asyncio.create_task(try_rpc(Config.RPC))
        backup_task = asyncio.create_task(try_rpc(Config.BACKUP_RPC, 5))
        
        # Wait for the first response or both to fail
        done, pending = await asyncio.wait(
            [primary_task, backup_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel pending task
        for task in pending:
            task.cancel()
        
        # Process the completed response
        response_json = None
        for task in done:
            result = await task
            if result:
                response_json = result
                break
            
        if response_json and 'result' in response_json and 'value' in response_json['result']:
            accounts = response_json['result']['value']
            
            logger.info(f"Found {len(accounts)} token accounts for {mint_str}")
            
            if accounts:
                for account in accounts:
                    if 'account' in account and 'data' in account['account']:
                        data = account['account']['data']
                        if 'parsed' in data and 'info' in data['parsed']:
                            info = data['parsed']['info']
                            if 'tokenAmount' in info:
                                amount = info['tokenAmount'].get('amount')
                                decimals = info['tokenAmount'].get('decimals', 0)
                                if amount:
                                    token_amount = int(amount) / (10 ** decimals)
                                    return int(amount), token_amount
        
        return 0, 0
    except Exception as e:
        logger.error(f"Error getting token balance: {e}")
        return 0, 0

def get_token_balance(mint_str):
    """Synchronous wrapper for async token balance function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(get_token_balance_async(mint_str))
    finally:
        loop.close()

async def get_sol_balance_async():
    """Get SOL balance for the current wallet asynchronously"""
    try:
        # Try both RPCs in parallel
        tasks = [
            asyncio.create_task(_get_balance_from_client(solana_client)),
            asyncio.create_task(_get_balance_from_client(backup_client))
        ]
        
        # Wait for the first successful result
        for completed in asyncio.as_completed(tasks):
            try:
                balance = await completed
                if balance is not None:
                    # Cancel remaining tasks
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    return balance
            except Exception:
                continue
                
        # If all tasks failed
        return 0
    except Exception as e:
        logger.error(f"Error getting SOL balance: {e}")
        return 0

async def _get_balance_from_client(client):
    """Helper function to get balance from a specific client"""
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            _thread_pool,
            lambda: client.get_balance(payer_keypair.pubkey())
        )
        sol_balance = resp.value / 1e9
        return sol_balance
    except Exception:
        return None

def get_sol_balance():
    """Synchronous wrapper for SOL balance check that works in all contexts"""
    try:
        # Direct HTTP call instead of using the solana client
        headers = {"Content-Type": "application/json"}
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(payer_keypair.pubkey())]
        }
        
        # Try primary RPC first
        try:
            response = requests.post(Config.RPC, headers=headers, json=payload, timeout=5)
            if response.status_code == 200:
                result = response.json()
                if "result" in result and "value" in result["result"]:
                    return result["result"]["value"] / 1e9
        except Exception:
            pass
            
        # Try backup RPC as fallback
        response = requests.post(Config.BACKUP_RPC, headers=headers, json=payload, timeout=5)
        if response.status_code == 200:
            result = response.json()
            if "result" in result and "value" in result["result"]:
                return result["result"]["value"] / 1e9
                
        # If we get here, both RPC calls failed
        return 0
    except Exception as e:
        logger.error(f"Error getting SOL balance: {e}")
        return 0

async def check_status_async(txn_sig, client, rpc_name="Unknown RPC"):
    """Check transaction status using a specific client (for parallel checking)."""
    try:
        loop = asyncio.get_event_loop()
        status_res = await loop.run_in_executor(
            _thread_pool,
            lambda: client.get_signature_statuses([txn_sig])
        )
        
        if status_res and status_res.value and status_res.value[0]:
            status = status_res.value[0]
            if status:
                if status.confirmation_status in ["confirmed", "finalized"]:
                    return True, status.confirmation_status, rpc_name
                elif status.confirmations is not None and status.confirmations > 0:
                    return True, f"{status.confirmations} confirmations", rpc_name
                elif status.err:
                    return False, f"Error: {status.err}", rpc_name
        return None, "pending", rpc_name
    except Exception as e:
        return None, f"Error: {e}", rpc_name

async def confirm_transaction_async(txn_sig, max_retries=None, retry_interval=None):
    """Optimized transaction confirmation with exponential backoff and parallel checks"""
    # Use more aggressive values
    max_retries = max_retries or 20  # Increase from 10
    initial_retry_interval = 0.05  # Start fast
    
    logger.info(f"CONFIRMING TRANSACTION...")
    logger.info(f"ID: {txn_sig}")
    
    # Start timing
    start_time = time.time()
    
    # For both RPCs in parallel
    headers = {"Content-Type": "application/json"}
    
    for attempt in range(1, max_retries + 1):
        # Calculate backoff with jitter - smarter backoff
        retry_interval = min(initial_retry_interval * (1.2 ** attempt), 2.0)
        jitter = random.uniform(0.8, 1.2)
        actual_interval = retry_interval * jitter
        
        # Log current attempt
        elapsed_time = time.time() - start_time
        logger.info(f"Check {attempt}/{max_retries}... (Elapsed: {elapsed_time:.2f}s)")
        
        # Direct method - getTransaction is more reliable than getSignatureStatuses
        payload = {
            "jsonrpc": "2.0", 
            "id": 1,
            "method": "getTransaction",
            "params": [
                str(txn_sig),
                {"encoding": "json", "maxSupportedTransactionVersion": 0}
            ]
        }
        
        # Try primary RPC
        try:
            response = requests.post(Config.RPC, headers=headers, json=payload, timeout=3)
            if response.status_code == 200:
                result = response.json()
                if "result" in result and result["result"]:
                    # Success!
                    confirm_time = time.time() - start_time
                    logger.info("=" * 50)
                    logger.info(f"TRANSACTION CONFIRMED!")
                    logger.info(f"Confirmation time: {confirm_time:.3f} seconds")
                    logger.info(f"Time: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
                    logger.info("=" * 50)
                    return True
        except Exception:
            pass
            
        # Try backup RPC right away if primary failed
        try:
            backup_response = requests.post(Config.BACKUP_RPC, headers=headers, json=payload, timeout=3)
            if backup_response.status_code == 200:
                backup_result = backup_response.json()
                if "result" in backup_result and backup_result["result"]:
                    # Success!
                    confirm_time = time.time() - start_time
                    logger.info("=" * 50)
                    logger.info(f"TRANSACTION CONFIRMED!")
                    logger.info(f"Confirmation time: {confirm_time:.3f} seconds")
                    logger.info(f"Time: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
                    logger.info("=" * 50)
                    return True
        except Exception:
            pass
        
        # If not confirmed yet, try a different method as well (getSignatureStatuses)
        try:
            status_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[str(txn_sig)], {"searchTransactionHistory": True}]
            }
            
            status_response = requests.post(Config.RPC, headers=headers, json=status_payload, timeout=3)
            if status_response.status_code == 200:
                status_result = status_response.json()
                if "result" in status_result and status_result["result"]["value"][0]:
                    status = status_result["result"]["value"][0]
                    if status and status.get("confirmationStatus") in ["confirmed", "finalized"]:
                        # Success through status check!
                        confirm_time = time.time() - start_time
                        logger.info("=" * 50)
                        logger.info(f"TRANSACTION CONFIRMED!")
                        logger.info(f"Confirmation time: {confirm_time:.3f} seconds")
                        logger.info(f"Time: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
                        logger.info("=" * 50)
                        return True
        except Exception:
            pass
        
        # Only wait if we're going to do another attempt
        if attempt < max_retries:
            logger.info(f"Waiting {actual_interval:.2f}s before next check...")
            time.sleep(actual_interval)
    
    # If we reached here, confirmation failed
    logger.warning(f"Confirmation timeout after {max_retries} attempts ({time.time() - start_time:.2f}s)")
    return False

def confirm_transaction(txn_sig, max_retries=None, retry_interval=None):
    """Synchronous wrapper for async confirmation function"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(confirm_transaction_async(txn_sig, max_retries, retry_interval))
    finally:
        loop.close()