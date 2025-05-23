#!/usr/bin/env python3
"""
Configuration, logging, and helper functions for the Solana Trading Bot
"""
import os
import logging
import json
import time
import re
from datetime import datetime
from dotenv import load_dotenv
import signal
import threading

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("trading_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Global stop event for graceful shutdown
stop_event = threading.Event()

# ================ CONFIGURATION ================
class Config:
    """Configuration class to manage all bot settings"""
    
    # Solana Configuration
    RPC = os.getenv("SOLANA_RPC_URL")
    BACKUP_RPC = os.getenv("BACKUP_RPC_URL")
    SOL = os.getenv("SOL_MINT_ADDRESS")
    
    # Telegram API credentials
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")
    
    # Trading Configuration
    BUY_AMOUNT = float(os.getenv("BUY_AMOUNT", 0.05))
    AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"
    AUTO_SELL = os.getenv("AUTO_SELL", "true").lower() == "true"
    
    # Auto-Sell Configuration
    PROFIT_TARGET = float(os.getenv("PROFIT_TARGET", 50.0))
    STOP_LOSS = float(os.getenv("STOP_LOSS", -20.0))
    TRAILING_STOP = float(os.getenv("TRAILING_STOP", 15.0))
    MAX_HOLD_TIME = int(os.getenv("MAX_HOLD_TIME", 12 * 60 * 60))
    
    # Exit Strategy Configuration
    EXIT_STRATEGY = os.getenv("EXIT_STRATEGY", "TRAILING_STOP")
    
    # Transaction Configuration
    DEFAULT_SLIPPAGE = float(os.getenv("DEFAULT_SLIPPAGE", 1.0))
    MAX_SWAP_RETRIES = int(os.getenv("MAX_SWAP_RETRIES", 5))  # Increased from 3
    PRICE_IMPACT_WARNING = float(os.getenv("PRICE_IMPACT_WARNING", 5.0))
    PRICE_IMPACT_ABORT = float(os.getenv("PRICE_IMPACT_ABORT", 10.0))
    
    # Priority Fee Configuration
    PRIORITY_FEE_MODE = os.getenv("PRIORITY_FEE_MODE", "auto")
    PRIORITY_FEE_LAMPORTS = int(os.getenv("PRIORITY_FEE_LAMPORTS", 500000))  # Increased
    
    # Price Tracking Configuration
    PRICE_CHECK_INTERVAL = float(os.getenv("PRICE_CHECK_INTERVAL", 0.1))  # Decreased
    PRICE_DISPLAY_INTERVAL = float(os.getenv("PRICE_DISPLAY_INTERVAL", 5))
    PRICE_CHECK_DURATION = int(os.getenv("PRICE_CHECK_DURATION", 24 * 60 * 60))
    
    # API Rate Limiting Configuration
    RATE_LIMIT_REQUESTS_PER_MINUTE = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", 180))  # Increased
    RATE_LIMIT_DELAY = 60 / RATE_LIMIT_REQUESTS_PER_MINUTE + 0.1  # Add a small buffer
    MAX_RATE_LIMIT_RETRIES = int(os.getenv("MAX_RATE_LIMIT_RETRIES", 3))
    
    # Transaction confirmation settings
    DEFAULT_COMMITMENT = os.getenv("DEFAULT_COMMITMENT", "processed")
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 15))
    RETRY_INTERVAL = float(os.getenv("RETRY_INTERVAL", 0.3))
    PARALLEL_RPC_CHECKS = os.getenv("PARALLEL_RPC_CHECKS", "true").lower() == "true"
    SHOW_RPC_DETAILS = os.getenv("SHOW_RPC_DETAILS", "true").lower() == "true"
    
    # NEW: Liquidity and Safety Settings
    MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", 1000))
    MAX_POSITION_SIZE_PERCENT = float(os.getenv("MAX_POSITION_SIZE_PERCENT", 5))
    ENABLE_PARTIAL_FILLS = os.getenv("ENABLE_PARTIAL_FILLS", "true").lower() == "true"
    CHECK_HONEYPOT = os.getenv("CHECK_HONEYPOT", "true").lower() == "true"
    
    # NEW: Multi-Level Take Profit Settings
    ENABLE_MULTI_TP = os.getenv("ENABLE_MULTI_TP", "true").lower() == "true"
    TP_LEVELS = os.getenv("TP_LEVELS", "25:30,25:50,25:100,25:200")  # format: percentage:profit
    
    # NEW: Volume Monitoring Settings
    ENABLE_VOLUME_MONITORING = os.getenv("ENABLE_VOLUME_MONITORING", "true").lower() == "true"
    VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", 3.0))
    VOLUME_DRYUP_PERCENT = float(os.getenv("VOLUME_DRYUP_PERCENT", 10.0))
    
    # NEW: WebSocket Settings
    ENABLE_WEBSOCKET = os.getenv("ENABLE_WEBSOCKET", "false").lower() == "true"
    WEBSOCKET_RPC = os.getenv("WEBSOCKET_RPC", "wss://api.mainnet-beta.solana.com")
    
    # NEW: Advanced Trading Settings
    USE_ORDER_SPLITTING = os.getenv("USE_ORDER_SPLITTING", "true").lower() == "true"
    SPLIT_THRESHOLD_IMPACT = float(os.getenv("SPLIT_THRESHOLD_IMPACT", 2.0))
    
    # Parse multi-level TP configuration
    @classmethod
    def parse_tp_levels(cls):
        """Parse the TP_LEVELS string into a list of tuples"""
        if not cls.ENABLE_MULTI_TP:
            return []
        
        levels = []
        for level in cls.TP_LEVELS.split(','):
            if ':' in level:
                pct, profit = level.split(':')
                levels.append((int(pct), float(profit)))
        return levels
    
    # Global state variables
    processed_addresses = set()
    price_tracking = {}  # Store tracking data: {token_address: {'buy_price': x, 'current_price': y, 'timestamp': z}}
    price_tracking_tasks = {}  # Store active tracking tasks
    auto_sell_locks = {}  # Prevent multiple sells of the same token
    api_last_call_time = {}  # Track last call time for each API endpoint 
    api_rate_limit_counters = {}  # Count consecutive rate limit errors
    
    # NEW: Performance tracking
    last_telegram_message_time = datetime.now()
    rpc_health_status = {"primary": True, "backup": True}
    websocket_connected = False
    
    @classmethod
    def save_to_env(cls):
        """Save current configuration back to .env file"""
        # Read existing .env file first to preserve comments
        with open('.env', 'r') as f:
            lines = f.readlines()
        
        # Dictionary to track updated keys
        updated_keys = set()
        
        # Update existing lines
        for i, line in enumerate(lines):
            if line.strip() and not line.strip().startswith('#'):
                key = line.split('=')[0].strip()
                value = getattr(cls, key, None)
                
                if value is not None:
                    # Convert value to appropriate string format
                    if isinstance(value, bool):
                        value_str = str(value).lower()
                    else:
                        value_str = str(value)
                    
                    lines[i] = f"{key}={value_str}\n"
                    updated_keys.add(key)
        
        # Add new configuration parameters if not present
        new_params = {
            'MIN_LIQUIDITY_USD': str(cls.MIN_LIQUIDITY_USD),
            'MAX_POSITION_SIZE_PERCENT': str(cls.MAX_POSITION_SIZE_PERCENT),
            'ENABLE_PARTIAL_FILLS': str(cls.ENABLE_PARTIAL_FILLS).lower(),
            'CHECK_HONEYPOT': str(cls.CHECK_HONEYPOT).lower(),
            'ENABLE_MULTI_TP': str(cls.ENABLE_MULTI_TP).lower(),
            'TP_LEVELS': cls.TP_LEVELS,
            'ENABLE_VOLUME_MONITORING': str(cls.ENABLE_VOLUME_MONITORING).lower(),
            'VOLUME_SPIKE_MULTIPLIER': str(cls.VOLUME_SPIKE_MULTIPLIER),
            'VOLUME_DRYUP_PERCENT': str(cls.VOLUME_DRYUP_PERCENT),
            'ENABLE_WEBSOCKET': str(cls.ENABLE_WEBSOCKET).lower(),
            'WEBSOCKET_RPC': cls.WEBSOCKET_RPC,
            'USE_ORDER_SPLITTING': str(cls.USE_ORDER_SPLITTING).lower(),
            'SPLIT_THRESHOLD_IMPACT': str(cls.SPLIT_THRESHOLD_IMPACT)
        }
        
        # Add new parameters at the end if they don't exist
        for key, value in new_params.items():
            if key not in updated_keys:
                lines.append(f"{key}={value}\n")
        
        # Write back to .env file
        with open('.env', 'w') as f:
            f.writelines(lines)
            
        logger.info("Configuration saved to .env file")
        
    @classmethod
    def update_param(cls, param_name, value):
        """Update a parameter value and save to .env"""
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
            
            # Save changes to .env
            cls.save_to_env()
            return True
        else:
            logger.error(f"Parameter {param_name} not found in configuration")
            return False
            
    @classmethod
    def show_current_parameters(cls):
        """Display current trading parameters"""
        logger.info("\n" + "=" * 50)
        logger.info("CURRENT TRADING PARAMETERS")
        logger.info("=" * 50)
        
        # Trading settings
        logger.info("TRADING SETTINGS:")
        logger.info(f"Buy Amount: {cls.BUY_AMOUNT} SOL")
        logger.info(f"Auto-Trade: {'Enabled' if cls.AUTO_TRADE else 'Disabled'}")
        logger.info(f"Auto-Sell: {'Enabled' if cls.AUTO_SELL else 'Disabled'}")
        
        # Exit strategy settings
        logger.info("\nEXIT STRATEGY SETTINGS:")
        logger.info(f"Strategy: {cls.EXIT_STRATEGY}")
        logger.info(f"Profit Target: {cls.PROFIT_TARGET}%")
        logger.info(f"Stop Loss: {cls.STOP_LOSS}%")
        logger.info(f"Trailing Stop: {cls.TRAILING_STOP} percentage points below highest ROI")
        if cls.EXIT_STRATEGY == "TRAILING_STOP":
            logger.info(f"Example: If highest profit reaches 75% and trailing stop is {cls.TRAILING_STOP}, "
                      f"exit will be at {75-cls.TRAILING_STOP}% profit")
        logger.info(f"Max Hold Time: {cls.MAX_HOLD_TIME/3600} hours")
        
        # Multi-TP settings
        if cls.ENABLE_MULTI_TP:
            logger.info("\nMULTI-LEVEL TAKE PROFIT:")
            for pct, profit in cls.parse_tp_levels():
                logger.info(f"Sell {pct}% at {profit}% profit")
        
        # Safety settings
        logger.info("\nSAFETY SETTINGS:")
        logger.info(f"Min Liquidity: ${cls.MIN_LIQUIDITY_USD}")
        logger.info(f"Max Position Size: {cls.MAX_POSITION_SIZE_PERCENT}% of liquidity")
        logger.info(f"Check Honeypot: {'Enabled' if cls.CHECK_HONEYPOT else 'Disabled'}")
        
        # Transaction settings
        logger.info("\nTRANSACTION SETTINGS:")
        logger.info(f"Default Slippage: {cls.DEFAULT_SLIPPAGE}%")
        logger.info(f"Price Impact Warning: {cls.PRICE_IMPACT_WARNING}%")
        logger.info(f"Price Impact Abort: {cls.PRICE_IMPACT_ABORT}%")
        logger.info(f"Priority Fee Mode: {cls.PRIORITY_FEE_MODE}")
        if cls.PRIORITY_FEE_MODE == "custom":
            logger.info(f"Priority Fee: {cls.PRIORITY_FEE_LAMPORTS} lamports ({cls.PRIORITY_FEE_LAMPORTS/1e9:.9f} SOL)")
        
        # Advanced settings
        logger.info("\nADVANCED SETTINGS:")
        logger.info(f"Order Splitting: {'Enabled' if cls.USE_ORDER_SPLITTING else 'Disabled'}")
        logger.info(f"Volume Monitoring: {'Enabled' if cls.ENABLE_VOLUME_MONITORING else 'Disabled'}")
        logger.info(f"WebSocket: {'Enabled' if cls.ENABLE_WEBSOCKET else 'Disabled'}")
        
        # Retry settings
        logger.info("\nRETRY SETTINGS:")
        logger.info(f"Max Swap Retries: {cls.MAX_SWAP_RETRIES}")
        logger.info(f"Max Rate Limit Retries: {cls.MAX_RATE_LIMIT_RETRIES}")
        logger.info(f"Transaction Confirmation Checks: {cls.MAX_RETRIES}")
        
        # API rate limit settings
        logger.info("\nAPI RATE LIMIT SETTINGS:")
        logger.info(f"Requests Per Minute: {cls.RATE_LIMIT_REQUESTS_PER_MINUTE}")
        logger.info(f"Delay Between Calls: {cls.RATE_LIMIT_DELAY:.2f}s")
        
        # Price monitoring settings
        logger.info("\nPRICE MONITORING SETTINGS:")
        logger.info(f"Check Interval: {cls.PRICE_CHECK_INTERVAL}s")
        logger.info(f"Display Interval: {cls.PRICE_DISPLAY_INTERVAL}s")
        
        logger.info("-" * 50)

# ================ HELPER FUNCTIONS ================
def extract_solana_addresses(text):
    """
    Extract Solana token addresses from text.
    Looks for strings that match Solana address pattern (base58, 32-44 chars).
    """
    # Pattern to match potential Solana addresses
    pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    
    # Find all matches
    matches = re.findall(pattern, text)
    
    # Filter to valid address length range
    filtered = []
    for match in matches:
        # Accept addresses of any length between 32-44 chars
        if 32 <= len(match) <= 44:
            filtered.append(match)
    
    # Print all accepted addresses
    if filtered:
        logger.info(f"Accepted {len(filtered)} valid Solana addresses: {filtered}")
    
    return filtered

def setup_signal_handlers():
    """Setup handlers for graceful shutdown on signals"""
    def signal_handler(sig, frame):
        logger.info("Received signal to stop. Shutting down gracefully...")
        stop_event.set()
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

# Interactive config menu functions
def change_buy_amount():
    """Change the default buy amount"""
    try:
        current = Config.BUY_AMOUNT
        print(f"Current buy amount: {current} SOL")
        new_amount = float(input("Enter new buy amount in SOL: "))
        if new_amount <= 0:
            print("Buy amount must be greater than 0")
            return
        Config.update_param('BUY_AMOUNT', new_amount)
        print(f"Buy amount changed to {Config.BUY_AMOUNT} SOL")
    except ValueError:
        print("Please enter a valid number")

def change_profit_target():
    """Change the profit target percentage"""
    try:
        current = Config.PROFIT_TARGET
        print(f"Current profit target: {current}%")
        new_target = float(input("Enter new profit target percentage: "))
        if new_target <= 0:
            print("Profit target must be greater than 0")
            return
        Config.update_param('PROFIT_TARGET', new_target)
        print(f"Profit target changed to {Config.PROFIT_TARGET}%")
    except ValueError:
        print("Please enter a valid number")

def change_stop_loss():
    """Change the stop loss percentage"""
    try:
        current = Config.STOP_LOSS
        print(f"Current stop loss: {current}%")
        new_stop = float(input("Enter new stop loss percentage (negative number): "))
        if new_stop >= 0:
            print("Stop loss must be a negative number")
            return
        Config.update_param('STOP_LOSS', new_stop)
        print(f"Stop loss changed to {Config.STOP_LOSS}%")
    except ValueError:
        print("Please enter a valid number")

def change_trailing_stop():
    """Change the trailing stop percentage points"""
    try:
        current = Config.TRAILING_STOP
        print(f"Current trailing stop: {current} percentage points")
        print(f"Example: If highest profit reaches 75% and trailing stop is 10, exit will be at 65% profit")
        new_stop = float(input("Enter new trailing stop percentage points: "))
        if new_stop <= 0:
            print("Trailing stop must be greater than 0")
            return
        Config.update_param('TRAILING_STOP', new_stop)
        print(f"Trailing stop changed to {Config.TRAILING_STOP} percentage points")
    except ValueError:
        print("Please enter a valid number")

def change_exit_strategy():
    """Change the exit strategy settings"""
    print("\nEXIT STRATEGY OPTIONS:")
    print("1. Full Sell at Take Profit (FULL_TP)")
    print("2. Trailing Stop Only (TRAILING_STOP)")
    print("3. Multi-Level Take Profit (MULTI_TP)")
    print(f"Current strategy: {Config.EXIT_STRATEGY}")
    
    try:
        choice = int(input("Select exit strategy (1-3): "))
        
        if choice == 1:
            Config.update_param('EXIT_STRATEGY', 'FULL_TP')
            Config.update_param('ENABLE_MULTI_TP', False)
        elif choice == 2:
            Config.update_param('EXIT_STRATEGY', 'TRAILING_STOP')
            Config.update_param('ENABLE_MULTI_TP', False)
        elif choice == 3:
            Config.update_param('EXIT_STRATEGY', 'MULTI_TP')
            Config.update_param('ENABLE_MULTI_TP', True)
            # Configure TP levels
            print("Configure take profit levels (format: percentage:profit)")
            print("Example: 25:30 means sell 25% at 30% profit")
            tp_levels = input("Enter TP levels (comma-separated): ")
            Config.update_param('TP_LEVELS', tp_levels)
        else:
            print("Invalid selection")
            return
            
        print(f"Exit strategy changed to: {Config.EXIT_STRATEGY}")
    except ValueError:
        print("Please enter a valid number")

def change_priority_fee_settings():
    """Change the priority fee settings"""
    print("\nPRIORITY FEE OPTIONS:")
    print("1. Auto (Jupiter automatically adjusts based on network conditions)")
    print("2. Custom (Set a fixed fee in lamports)")
    print(f"Current setting: {Config.PRIORITY_FEE_MODE}")
    if Config.PRIORITY_FEE_MODE == "custom":
        print(f"Current fee: {Config.PRIORITY_FEE_LAMPORTS} lamports ({Config.PRIORITY_FEE_LAMPORTS/1e9:.9f} SOL)")
    
    try:
        choice = int(input("Select priority fee mode (1-2): "))
        
        if choice == 1:
            Config.update_param('PRIORITY_FEE_MODE', 'auto')
            print("Priority fee mode set to auto")
        elif choice == 2:
            Config.update_param('PRIORITY_FEE_MODE', 'custom')
            # Get custom fee amount
            new_fee = int(input("Enter fee in lamports (recommended range: 100000-1000000): "))
            if new_fee <= 0:
                print("Fee must be greater than 0")
                return
            Config.update_param('PRIORITY_FEE_LAMPORTS', new_fee)
            print(f"Priority fee set to {Config.PRIORITY_FEE_LAMPORTS} lamports ({Config.PRIORITY_FEE_LAMPORTS/1e9:.9f} SOL)")
        else:
            print("Invalid selection")
            return
    except ValueError:
        print("Please enter a valid number")

def configure_all_parameters():
    """Configure all trading parameters in one flow"""
    print("\n" + "=" * 50)
    print("CONFIGURE ALL PARAMETERS")
    print("=" * 50)
    print("Press Enter to keep current value")
    print("-" * 50)
    
    try:
        # Trading settings
        print("\nTRADING SETTINGS:")
        new_buy_amount = input(f"Buy Amount ({Config.BUY_AMOUNT} SOL): ")
        if new_buy_amount.strip():
            Config.update_param('BUY_AMOUNT', float(new_buy_amount))
            
        # Exit strategy settings
        print("\nEXIT STRATEGY SETTINGS:")
        print("Exit Strategy Options:")
        print("1. Full Sell at Take Profit (FULL_TP)")
        print("2. Trailing Stop Only (TRAILING_STOP)")
        print("3. Multi-Level Take Profit (MULTI_TP)")
        print(f"Current: {Config.EXIT_STRATEGY}")
        new_strategy = input("Select exit strategy (1-3): ")
        if new_strategy.strip():
            if new_strategy == "1":
                Config.update_param('EXIT_STRATEGY', 'FULL_TP')
                Config.update_param('ENABLE_MULTI_TP', False)
            elif new_strategy == "2":
                Config.update_param('EXIT_STRATEGY', 'TRAILING_STOP')
                Config.update_param('ENABLE_MULTI_TP', False)
            elif new_strategy == "3":
                Config.update_param('EXIT_STRATEGY', 'MULTI_TP')
                Config.update_param('ENABLE_MULTI_TP', True)
            
        new_profit_target = input(f"Profit Target ({Config.PROFIT_TARGET}%): ")
        if new_profit_target.strip():
            Config.update_param('PROFIT_TARGET', float(new_profit_target))
            
        new_stop_loss = input(f"Stop Loss ({Config.STOP_LOSS}%): ")
        if new_stop_loss.strip():
            Config.update_param('STOP_LOSS', float(new_stop_loss))
            
        new_trailing_stop = input(f"Trailing Stop ({Config.TRAILING_STOP}%): ")
        if new_trailing_stop.strip():
            Config.update_param('TRAILING_STOP', float(new_trailing_stop))
            
        new_hold_hours = input(f"Max Hold Time ({Config.MAX_HOLD_TIME/3600} hours): ")
        if new_hold_hours.strip():
            Config.update_param('MAX_HOLD_TIME', float(new_hold_hours) * 3600)
            
        # Safety settings
        print("\nSAFETY SETTINGS:")
        new_min_liquidity = input(f"Min Liquidity USD ({Config.MIN_LIQUIDITY_USD}): ")
        if new_min_liquidity.strip():
            Config.update_param('MIN_LIQUIDITY_USD', float(new_min_liquidity))
            
        new_max_position = input(f"Max Position Size % ({Config.MAX_POSITION_SIZE_PERCENT}): ")
        if new_max_position.strip():
            Config.update_param('MAX_POSITION_SIZE_PERCENT', float(new_max_position))
            
        # Transaction settings
        print("\nTRANSACTION SETTINGS:")
        new_slippage = input(f"Default Slippage ({Config.DEFAULT_SLIPPAGE}%): ")
        if new_slippage.strip():
            Config.update_param('DEFAULT_SLIPPAGE', float(new_slippage))
            
        new_impact_warning = input(f"Price Impact Warning ({Config.PRICE_IMPACT_WARNING}%): ")
        if new_impact_warning.strip():
            Config.update_param('PRICE_IMPACT_WARNING', float(new_impact_warning))
            
        new_impact_abort = input(f"Price Impact Abort ({Config.PRICE_IMPACT_ABORT}%): ")
        if new_impact_abort.strip():
            Config.update_param('PRICE_IMPACT_ABORT', float(new_impact_abort))
            
        # Priority Fee Settings
        print("\nPRIORITY FEE SETTINGS:")
        print("1. Auto (Jupiter automatically adjusts)")
        print("2. Custom (Set a fixed fee)")
        print(f"Current: {Config.PRIORITY_FEE_MODE}")
        new_pf_mode = input("Select priority fee mode (1-2): ")
        if new_pf_mode.strip():
            if new_pf_mode == "1":
                Config.update_param('PRIORITY_FEE_MODE', 'auto')
            elif new_pf_mode == "2":
                Config.update_param('PRIORITY_FEE_MODE', 'custom')
                new_pf_lamports = input(f"Priority Fee in lamports ({Config.PRIORITY_FEE_LAMPORTS}): ")
                if new_pf_lamports.strip():
                    Config.update_param('PRIORITY_FEE_LAMPORTS', int(new_pf_lamports))
        
        print("\nAll parameters updated successfully!")
        
    except ValueError as e:
        print(f"Error: {e}")
        print("Some parameters were not updated due to invalid input")
    
    # Show the updated parameters
    Config.show_current_parameters()