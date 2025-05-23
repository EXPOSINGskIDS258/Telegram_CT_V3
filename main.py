#!/usr/bin/env python3
"""
Enhanced Solana Trading Bot with Auto-Sell and Paper Trading
- Monitors multiple Telegram groups for Solana token addresses
- Supports both real trading and paper trading modes
- Automatically trades tokens using Jupiter DEX with optimized speed
- Tracks price changes after purchase
- Auto-sells tokens based on profit targets or stop-loss conditions
- Multiple exit strategies: Full TP, Trailing Stop
- RPC performance measurements for optimizing transactions
- Secure license system to prevent unauthorized use
- Beautiful CLI interface with real-time updates
"""
import asyncio
import os
import time
from datetime import datetime
import signal
import sys
import threading
import base64
import requests
import websockets

# Rich console imports for beautiful CLI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.layout import Layout
from rich.text import Text

# Check security first before importing other modules
try:
    from security import check_activation
    
    # Check if the software is activated
    if not check_activation():
        console = Console()
        console.print(Panel.fit(
            "This software requires activation. Please restart the application with a valid activation code.",
            title="[bold red]ACTIVATION REQUIRED[/]", 
            border_style="red"
        ))
        sys.exit(1)
except ImportError:
    console = Console()
    console.print(Panel.fit(
        "The security module could not be found. The software cannot start without the security module.",
        title="[bold red]ERROR[/]", 
        border_style="red"
    ))
    sys.exit(1)

# Import other modules only after activation is confirmed
from config import Config, logger, setup_signal_handlers
from solanaa import initialize_clients, initialize_wallet, get_sol_balance, get_token_balance_async
from telegram import initialize_telegram, fetch_channels, setup_telegram_handler, get_active_chats, add_new_chats, remove_chats
from trading import start_price_tracking, show_active_tracking
from config import change_buy_amount, change_profit_target, change_stop_loss
from config import change_trailing_stop, change_exit_strategy, change_priority_fee_settings
from config import configure_all_parameters, stop_event
from jupiter import execute_swap, execute_swap_async
from database import db  # Import database module

# Import paper trading module
from paper_trading import PaperConfig, initialize_paper_trading
from paper_trading import measure_rpc_delay, measure_rpc_delay_sync
from paper_trading import paper_buy_token, paper_buy_token_async
from paper_trading import paper_sell_token, paper_sell_token_async
from paper_trading import show_paper_portfolio

# Initialize rich console for beautiful output
console = Console()

# ================ HEARTBEAT & MONITORING FUNCTIONS ================
async def heartbeat_monitor():
    """Monitor bot health and send alerts"""
    last_rpc_check = datetime.now()
    last_db_stats = datetime.now()
    
    while not stop_event.is_set():
        try:
            current_time = datetime.now()
            
            # Check if bot is still receiving messages
            if (current_time - Config.last_telegram_message_time).seconds > 1800:  # 30 minutes
                logger.warning("No Telegram messages for 30 minutes! Checking connection...")
                
                # Check if Telegram is still connected
                global telegram_client
                if telegram_client and not telegram_client.is_connected():
                    logger.warning("Telegram disconnected, attempting reconnection...")
                    try:
                        await telegram_client.connect()
                        logger.info("Telegram reconnected successfully")
                    except Exception as e:
                        logger.error(f"Failed to reconnect Telegram: {e}")
            
            # Check RPC health every 5 minutes
            if (current_time - last_rpc_check).seconds > 300:
                last_rpc_check = current_time
                rpc_health = await check_rpc_health()
                
                if not rpc_health['primary']:
                    logger.warning("Primary RPC unhealthy, switching to backup")
                    Config.RPC, Config.BACKUP_RPC = Config.BACKUP_RPC, Config.RPC
                    Config.save_to_env()
                    
                    # Reinitialize clients with new RPC
                    from solanaa import initialize_clients
                    initialize_clients(Config.RPC)
            
            # Log database statistics every hour
            if (current_time - last_db_stats).seconds > 3600:
                last_db_stats = current_time
                stats = db.get_trade_statistics()
                if stats['closed_trades'] > 0:
                    logger.info(f"Hourly Stats - Trades: {stats['closed_trades']}, Win Rate: {stats['win_rate']:.1f}%, Total P/L: {stats['total_profit_loss']:.6f} SOL")
            
            # Check WebSocket connection if enabled
            if Config.ENABLE_WEBSOCKET and not Config.websocket_connected:
                logger.warning("WebSocket disconnected, will reconnect on next trade")
            
            # Sleep for 60 seconds
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in heartbeat monitor: {e}")
            await asyncio.sleep(60)

async def check_rpc_health() -> dict:
    """Check health of RPC endpoints"""
    health = {"primary": False, "backup": False}
    
    try:
        # Test primary RPC
        start = time.time()
        response = requests.post(
            Config.RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
            timeout=5
        )
        if response.status_code == 200 and (time.time() - start) < 2:
            health["primary"] = True
    except:
        pass
    
    try:
        # Test backup RPC
        start = time.time()
        response = requests.post(
            Config.BACKUP_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
            timeout=5
        )
        if response.status_code == 200 and (time.time() - start) < 2:
            health["backup"] = True
    except:
        pass
    
    return health

# ================ UI HELPER FUNCTIONS ================
def print_header(title, char="=", style="bold blue"):
    """Print a nice formatted header with title"""
    width = 80
    console.print(f"\n[{style}]{char * width}[/{style}]")
    console.print(f"[{style}]{title.center(width)}[/{style}]")
    console.print(f"[{style}]{char * width}[/{style}]")

def display_current_settings():
    """Display all current settings loaded from .env with enhanced UI"""
    # Create a table for trading settings
    table = Table(title="CURRENT CONFIGURATION", show_header=True, header_style="bold blue")
    table.add_column("Setting", style="cyan", justify="right")
    table.add_column("Value", style="green")
    
    # Trading settings
    table.add_row("[bold]Trading Settings[/]", "")
    table.add_row("Buy Amount", f"{Config.BUY_AMOUNT} SOL")
    table.add_row("Auto-Trading", f"{'[green]Enabled[/]' if Config.AUTO_TRADE else '[red]Disabled[/]'}")
    table.add_row("Auto-Selling", f"{'[green]Enabled[/]' if Config.AUTO_SELL else '[red]Disabled[/]'}")
    
    # Paper trading settings
    table.add_row("[bold]Paper Trading Settings[/]", "")
    table.add_row("Paper Trading", f"{'[green]Enabled[/]' if PaperConfig.PAPER_ENABLED else '[red]Disabled[/]'}")
    table.add_row("Paper SOL Balance", f"{PaperConfig.PAPER_SOL_BALANCE} SOL")
    table.add_row("Paper Buy Amount", f"{PaperConfig.PAPER_BUY_AMOUNT} SOL")
    table.add_row("Paper Auto-Trading", f"{'[green]Enabled[/]' if PaperConfig.PAPER_AUTO_TRADE else '[red]Disabled[/]'}")
    table.add_row("Paper Auto-Selling", f"{'[green]Enabled[/]' if PaperConfig.PAPER_AUTO_SELL else '[red]Disabled[/]'}")
    
    # Strategy settings
    table.add_row("[bold]Exit Strategy Settings[/]", "")
    table.add_row("Strategy", f"{Config.EXIT_STRATEGY}")
    table.add_row("Profit Target", f"{Config.PROFIT_TARGET}%")
    table.add_row("Stop Loss", f"{Config.STOP_LOSS}%")
    table.add_row("Trailing Stop", f"{Config.TRAILING_STOP}%")
    table.add_row("Max Hold Time", f"{Config.MAX_HOLD_TIME/3600} hours")
    
    # Safety settings
    table.add_row("[bold]Safety Settings[/]", "")
    table.add_row("Min Liquidity", f"${Config.MIN_LIQUIDITY_USD}")
    table.add_row("Max Position Size", f"{Config.MAX_POSITION_SIZE_PERCENT}% of liquidity")
    table.add_row("Check Honeypot", f"{'[green]Enabled[/]' if Config.CHECK_HONEYPOT else '[red]Disabled[/]'}")
    
    # Paper exit strategy settings (if different)
    if (PaperConfig.PAPER_PROFIT_TARGET is not None or 
        PaperConfig.PAPER_STOP_LOSS is not None or 
        PaperConfig.PAPER_TRAILING_STOP is not None or 
        PaperConfig.PAPER_MAX_HOLD_TIME is not None or
        PaperConfig.PAPER_EXIT_STRATEGY is not None):
        
        table.add_row("[bold]Paper Exit Strategy Settings[/]", "")
        
        if PaperConfig.PAPER_EXIT_STRATEGY is not None:
            table.add_row("Paper Strategy", f"{PaperConfig.PAPER_EXIT_STRATEGY}")
        
        if PaperConfig.PAPER_PROFIT_TARGET is not None:
            table.add_row("Paper Profit Target", f"{PaperConfig.PAPER_PROFIT_TARGET}%")
            
        if PaperConfig.PAPER_STOP_LOSS is not None:
            table.add_row("Paper Stop Loss", f"{PaperConfig.PAPER_STOP_LOSS}%")
            
        if PaperConfig.PAPER_TRAILING_STOP is not None:
            table.add_row("Paper Trailing Stop", f"{PaperConfig.PAPER_TRAILING_STOP}%")
            
        if PaperConfig.PAPER_MAX_HOLD_TIME is not None:
            table.add_row("Paper Max Hold Time", f"{PaperConfig.PAPER_MAX_HOLD_TIME/3600} hours")
    
    # Transaction settings
    table.add_row("[bold]Transaction Settings[/]", "")
    table.add_row("Slippage", f"{Config.DEFAULT_SLIPPAGE}%")
    table.add_row("Priority Fee", f"{Config.PRIORITY_FEE_MODE if Config.PRIORITY_FEE_MODE == 'auto' else f'Custom ({Config.PRIORITY_FEE_LAMPORTS} lamports)'}")
    table.add_row("Price Impact Warning", f"{Config.PRICE_IMPACT_WARNING}%")
    table.add_row("Price Impact Abort", f"{Config.PRICE_IMPACT_ABORT}%")
    
    # Advanced settings
    table.add_row("[bold]Advanced Settings[/]", "")
    table.add_row("Order Splitting", f"{'[green]Enabled[/]' if Config.USE_ORDER_SPLITTING else '[red]Disabled[/]'}")
    table.add_row("Volume Monitoring", f"{'[green]Enabled[/]' if Config.ENABLE_VOLUME_MONITORING else '[red]Disabled[/]'}")
    table.add_row("WebSocket", f"{'[green]Enabled[/]' if Config.ENABLE_WEBSOCKET else '[red]Disabled[/]'}")
    
    # RPC Performance
    table.add_row("[bold]RPC Performance[/]", "")
    table.add_row("Measured Delay", f"{PaperConfig.avg_measured_delay:.2f} ms")
    table.add_row("Last Measurement", f"{datetime.fromtimestamp(PaperConfig.last_delay_measurement).strftime('%H:%M:%S') if PaperConfig.last_delay_measurement > 0 else 'Never'}")
    
    # Network settings
    table.add_row("[bold]Network Settings[/]", "")
    table.add_row("RPC URL", f"{Config.RPC}")
    table.add_row("Backup RPC", f"{Config.BACKUP_RPC}")
    
    # Rate limit settings
    table.add_row("[bold]API Settings[/]", "")
    table.add_row("Rate Limit", f"{Config.RATE_LIMIT_REQUESTS_PER_MINUTE} req/min")
    table.add_row("Price Check Interval", f"{Config.PRICE_CHECK_INTERVAL}s")
    table.add_row("Price Display Interval", f"{Config.PRICE_DISPLAY_INTERVAL}s")
    
    console.print(table)

# Function to configure RPC settings
async def configure_rpc():
    """Configure Solana RPC endpoint with enhanced UI"""
    print_header("SOLANA RPC CONFIGURATION")
    
    # Create a panel with RPC information
    rpc_panel = Panel.fit(
        "Using a private RPC with higher rate limits is recommended for better performance.\n\n"
        "Options:\n"
        "[cyan]1.[/] Helius - [link=https://www.helius.dev/]https://www.helius.dev/[/link]\n"
        "[cyan]2.[/] QuickNode - [link=https://www.quicknode.com/]https://www.quicknode.com/[/link]\n"
        "[cyan]3.[/] Triton - [link=https://triton.one/]https://triton.one/[/link]\n"
        "[cyan]4.[/] Continue with current RPC settings\n\n"
        f"Current RPC: [bold]{Config.RPC}[/bold]",
        title="[bold cyan]RPC Options[/]", 
        border_style="cyan"
    )
    console.print(rpc_panel)
    
    choice = Prompt.ask("Select an option", choices=["1", "2", "3", "4"], default="4")
    
    if choice == "4":
        console.print(f"Continuing with current RPC: [bold]{Config.RPC}[/bold]")
        return Config.RPC
    
    rpc_url = Prompt.ask("\nEnter your RPC URL", default="")
    
    if rpc_url:
        Config.RPC = rpc_url
        Config.save_to_env()
        console.print(f"Using custom RPC: [bold green]{Config.RPC}[/bold green]")
    else:
        Config.RPC = Config.BACKUP_RPC
        Config.save_to_env()
        console.print(f"Using public RPC: [bold yellow]{Config.RPC}[/bold yellow] (Note: May have rate limiting issues)")
    
    # Test RPC speed
    if Confirm.ask("Would you like to test the RPC speed?", default=True):
        with console.status("[cyan]Measuring RPC response time...[/]"):
            delay_ms = measure_rpc_delay_sync()
        
        console.print(f"RPC Response Time: [bold]⏱ {delay_ms:.2f} ms[/]")
        
        # Give feedback on RPC performance
        if delay_ms < 100:
            console.print("[bold green]✓ Excellent performance![/] Expect fast trades.")
        elif delay_ms < 200:
            console.print("[bold green]✓ Good performance.[/] Trading should be responsive.")
        elif delay_ms < 500:
            console.print("[bold yellow]⚠ Average performance.[/] Some trades might experience delays.")
        else:
            console.print("[bold red]⚠ Slow performance![/] Consider using a different RPC for better results.")
    
    return Config.RPC

# Function to configure wallet settings
async def configure_wallet():
    """Configure wallet settings with enhanced UI"""
    print_header("WALLET CONFIGURATION")
    
    # Check for existing wallet file
    if os.path.exists("wallet.dat"):
        console.print("[bold green]Existing wallet file found.[/bold green]")
        use_existing = Confirm.ask("Use existing wallet?", default=True)
        
        if use_existing:
            # Try to load existing wallet
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold green]Loading existing wallet...[/]"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("Loading...", total=None)
                payer_keypair = initialize_wallet(None, use_existing=True)
                
            if not payer_keypair:
                console.print("[bold red]Could not load existing wallet.[/bold red]")
                private_key = Prompt.ask("Enter private key (or press Enter for new wallet)", password=True, default="")
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold green]Initializing wallet...[/]"),
                    console=console,
                    transient=True
                ) as progress:
                    task = progress.add_task("Initializing...", total=None)
                    payer_keypair = initialize_wallet(private_key if private_key.strip() else None)
        else:
            private_key = Prompt.ask("Enter private key (or press Enter for new wallet)", password=True, default="")
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold green]Initializing wallet...[/]"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("Initializing...", total=None)
                payer_keypair = initialize_wallet(private_key if private_key.strip() else None)
    else:
        console.print("[bold yellow]No existing wallet file found.[/bold yellow]")
        private_key = Prompt.ask("Enter private key (or press Enter for new wallet)", password=True, default="")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold green]Initializing wallet...[/]"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Initializing...", total=None)
            payer_keypair = initialize_wallet(private_key if private_key.strip() else None)
    
    # Check SOL balance
    try:
        console.print("[bold]Checking balance...[/]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Fetching SOL balance...[/]"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Fetching...", total=None)
            sol_balance = get_sol_balance()
        
        console.print(f"\nCurrent SOL balance: [bold green]{sol_balance}[/bold green] SOL")
        
        # Warn if balance is low
        if sol_balance < Config.BUY_AMOUNT:
            console.print(Panel.fit(
                f"Current: {sol_balance} SOL, Required for trading: {Config.BUY_AMOUNT} SOL",
                title="[bold red]LOW BALANCE WARNING[/]", 
                border_style="red"
            ))
            
            # Always offer paper trading when balance is low
            if not PaperConfig.PAPER_ENABLED:
                use_paper = Confirm.ask("Would you like to enable paper trading to test without real funds?", default=True)
                if use_paper:
                    PaperConfig.update_param('PAPER_ENABLED', True)
                    console.print("[bold green]✓[/] Paper trading enabled!")
            
            proceed = Confirm.ask("Continue anyway?", default=False)
            if not proceed:
                console.print("[bold red]Exiting...[/bold red]")
                return None
    except Exception as e:
        console.print(f"[bold red]Error checking SOL balance: {e}[/bold red]")
    
    return payer_keypair

# Function to configure Telegram settings
async def configure_telegram():
    """Configure Telegram credentials and connection with enhanced UI"""
    print_header("TELEGRAM CONFIGURATION")
    
    # Configure Telegram API credentials if not set
    if not all([Config.TELEGRAM_API_ID, Config.TELEGRAM_API_HASH, Config.TELEGRAM_PHONE]):
        console.print(Panel.fit(
            "You need to enter your Telegram API credentials to monitor groups.\n"
            "Get them from [link=https://my.telegram.org/apps]https://my.telegram.org/apps[/link] if you don't have them.",
            title="[bold yellow]TELEGRAM API REQUIRED[/]", 
            border_style="yellow"
        ))
        
        # Get Telegram API credentials
        Config.TELEGRAM_API_ID = Prompt.ask("Enter your Telegram API ID")
        Config.TELEGRAM_API_HASH = Prompt.ask("Enter your Telegram API Hash")
        Config.TELEGRAM_PHONE = Prompt.ask("Enter your phone number (with country code, e.g. +19295259432)")
        
        # Save to .env file
        Config.save_to_env()
    else:
        console.print(f"Using existing Telegram credentials:")
        console.print(f"• API ID: [bold]{Config.TELEGRAM_API_ID}[/bold]")
        console.print(f"• Phone: [bold]{Config.TELEGRAM_PHONE}[/bold]")
        
        # Option to update credentials
        update_creds = Confirm.ask("\nUpdate Telegram credentials?", default=False)
        
        if update_creds:
            Config.TELEGRAM_API_ID = Prompt.ask("Enter your Telegram API ID", default=Config.TELEGRAM_API_ID)
            Config.TELEGRAM_API_HASH = Prompt.ask("Enter your Telegram API Hash", default=Config.TELEGRAM_API_HASH)
            Config.TELEGRAM_PHONE = Prompt.ask("Enter your phone number (with country code, e.g. +19295259432)", default=Config.TELEGRAM_PHONE)
            Config.save_to_env()
    
    # Initialize Telegram client
    try:
        console.print("\n[bold]Connecting to Telegram...[/]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Initializing Telegram client...[/]"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Connecting...", total=None)
            telegram_client = await initialize_telegram()
        
        if not telegram_client:
            console.print("[bold red]Failed to initialize Telegram client.[/bold red]")
            return None, None
        
        # Fetch and select multiple groups
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Fetching available groups...[/]"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Fetching...", total=None)
            chat_list = await fetch_channels()  # This now returns a list of chats
        
        if not chat_list:
            console.print("[bold red]Failed to set up Telegram monitoring.[/bold red]")
            return None, None
        
        return telegram_client, chat_list
    except Exception as e:
        console.print(f"[bold red]Error setting up Telegram: {e}[/bold red]")
        
        tips_panel = Panel.fit(
            "1. Make sure your API ID and hash are correct\n"
            "2. Ensure your phone number is in the correct format (+19295259432)\n"
            "3. Check your internet connection",
            title="[bold yellow]TIPS FOR RESOLVING TELEGRAM ISSUES[/]", 
            border_style="yellow"
        )
        console.print(tips_panel)
        
        retry = Confirm.ask("\nRetry Telegram setup?", default=True)
        if retry:
            # Try with new credentials
            Config.TELEGRAM_API_ID = Prompt.ask("Enter your Telegram API ID", default=Config.TELEGRAM_API_ID)
            Config.TELEGRAM_API_HASH = Prompt.ask("Enter your Telegram API Hash", default=Config.TELEGRAM_API_HASH)
            Config.TELEGRAM_PHONE = Prompt.ask("Enter your phone number (with country code, e.g. +19295259432)", default=Config.TELEGRAM_PHONE)
            Config.save_to_env()
            
            return await configure_telegram()
        else:
            return None, None

# Function to configure trading parameters
async def configure_trading():
    """Configure trading parameters with enhanced UI"""
    print_header("TRADING CONFIGURATION")
    
    # Create a panel with current trading settings
    settings_panel = Panel.fit(
        f"• Buy Amount: [bold]{Config.BUY_AMOUNT}[/bold] SOL\n"
        f"• Auto-Trading: [bold {'green' if Config.AUTO_TRADE else 'red'}]{'Enabled' if Config.AUTO_TRADE else 'Disabled'}[/bold]\n"
        f"• Auto-Selling: [bold {'green' if Config.AUTO_SELL else 'red'}]{'Enabled' if Config.AUTO_SELL else 'Disabled'}[/bold]\n",
        title="[bold cyan]Current Trading Settings[/]", 
        border_style="cyan"
    )
    console.print(settings_panel)
    
    update_settings = Confirm.ask("Update trading settings?", default=False)
    
    if update_settings:
        # Trading settings
        new_buy_amount = Prompt.ask(
            f"Buy Amount ({Config.BUY_AMOUNT} SOL)", 
            default=str(Config.BUY_AMOUNT)
        )
        if new_buy_amount.strip(): 
            Config.update_param('BUY_AMOUNT', float(new_buy_amount))
            
        auto_trade_setting = Confirm.ask(
            f"Enable auto-trading?", 
            default=Config.AUTO_TRADE
        )
        Config.update_param('AUTO_TRADE', auto_trade_setting)
        
        auto_sell_setting = Confirm.ask(
            f"Enable auto-selling?", 
            default=Config.AUTO_SELL
        )
        Config.update_param('AUTO_SELL', auto_sell_setting)
        
        Config.save_to_env()
        console.print("[bold green]Trading settings updated successfully![/bold green]")
    else:
        console.print("[cyan]Keeping current trading settings.[/cyan]")

# Function to configure paper trading
async def configure_paper_trading():
    """Configure paper trading parameters with enhanced UI"""
    print_header("PAPER TRADING CONFIGURATION")
    
    # Create a panel with current paper trading settings
    settings_panel = Panel.fit(
        f"• Paper Trading: [bold {'green' if PaperConfig.PAPER_ENABLED else 'red'}]{'Enabled' if PaperConfig.PAPER_ENABLED else 'Disabled'}[/bold]\n"
        f"• Paper SOL Balance: [bold]{PaperConfig.PAPER_SOL_BALANCE}[/bold] SOL\n"
        f"• Paper Buy Amount: [bold]{PaperConfig.PAPER_BUY_AMOUNT}[/bold] SOL\n"
        f"• Paper Slippage: [bold]{PaperConfig.PAPER_SLIPPAGE}%[/bold]\n"
        f"• Paper Auto-Trading: [bold {'green' if PaperConfig.PAPER_AUTO_TRADE else 'red'}]{'Enabled' if PaperConfig.PAPER_AUTO_TRADE else 'Disabled'}[/bold]\n"
        f"• Paper Auto-Selling: [bold {'green' if PaperConfig.PAPER_AUTO_SELL else 'red'}]{'Enabled' if PaperConfig.PAPER_AUTO_SELL else 'Disabled'}[/bold]\n",
        title="[bold cyan]Current Paper Trading Settings[/]", 
        border_style="cyan"
    )
    console.print(settings_panel)
    
    # Measure RPC speed to show accurate simulation values
    if Confirm.ask("Measure current RPC speed (for realistic simulation)?", default=True):
        with console.status("[cyan]Measuring RPC response time...[/]"):
            delay_ms = measure_rpc_delay_sync()
        
        console.print(f"RPC Response Time: [bold]⏱ {delay_ms:.2f} ms[/]")
        
        # Give feedback on RPC performance
        if delay_ms < 100:
            console.print("[bold green]✓ Excellent performance![/] Paper trading will simulate this speed.")
        elif delay_ms < 200:
            console.print("[bold green]✓ Good performance.[/] Paper trading will simulate this speed.")
        elif delay_ms < 500:
            console.print("[bold yellow]⚠ Average performance.[/] Paper trading will simulate this speed.")
        else:
            console.print("[bold red]⚠ Slow performance![/] Paper trading will simulate this speed.")
    
    update_settings = Confirm.ask("Update paper trading settings?", default=True)
    
    if update_settings:
        # Paper trading settings
        paper_enabled = Confirm.ask(
            f"Enable paper trading?", 
            default=PaperConfig.PAPER_ENABLED
        )
        PaperConfig.update_param('PAPER_ENABLED', paper_enabled)
        
        new_paper_balance = Prompt.ask(
            f"Paper SOL Balance ({PaperConfig.PAPER_SOL_BALANCE} SOL)", 
            default=str(PaperConfig.PAPER_SOL_BALANCE)
        )
        if new_paper_balance.strip(): 
            PaperConfig.update_param('PAPER_SOL_BALANCE', float(new_paper_balance))
        
        new_paper_buy_amount = Prompt.ask(
            f"Paper Buy Amount ({PaperConfig.PAPER_BUY_AMOUNT} SOL)", 
            default=str(PaperConfig.PAPER_BUY_AMOUNT)
        )
        if new_paper_buy_amount.strip(): 
            PaperConfig.update_param('PAPER_BUY_AMOUNT', float(new_paper_buy_amount))
            
        new_paper_slippage = Prompt.ask(
            f"Paper Slippage ({PaperConfig.PAPER_SLIPPAGE}%)", 
            default=str(PaperConfig.PAPER_SLIPPAGE)
        )
        if new_paper_slippage.strip(): 
            PaperConfig.update_param('PAPER_SLIPPAGE', float(new_paper_slippage))
            
        paper_auto_trade = Confirm.ask(
            f"Enable paper auto-trading?", 
            default=PaperConfig.PAPER_AUTO_TRADE
        )
        PaperConfig.update_param('PAPER_AUTO_TRADE', paper_auto_trade)
        
        paper_auto_sell = Confirm.ask(
            f"Enable paper auto-selling?", 
            default=PaperConfig.PAPER_AUTO_SELL
        )
        PaperConfig.update_param('PAPER_AUTO_SELL', paper_auto_sell)
        
        # Ask if user wants to use separate exit strategy for paper trading
        use_separate_exit = Confirm.ask(
            f"Use separate exit strategy for paper trading?", 
            default=PaperConfig.PAPER_EXIT_STRATEGY is not None
        )
        
        if use_separate_exit:
            # Configure paper-specific exit strategy
            console.print("\n[bold cyan]PAPER EXIT STRATEGY OPTIONS:[/bold cyan]")
            console.print("1. Full Sell at Take Profit (FULL_TP)")
            console.print("2. Trailing Stop Only (TRAILING_STOP)")
            
            strategy_choice = Prompt.ask(
                "Select paper exit strategy", 
                choices=["1", "2"], 
                default="1" if PaperConfig.PAPER_EXIT_STRATEGY == "FULL_TP" or not PaperConfig.PAPER_EXIT_STRATEGY else "2"
            )
            
            if strategy_choice == "1":
                PaperConfig.update_param('PAPER_EXIT_STRATEGY', 'FULL_TP')
            else:
                PaperConfig.update_param('PAPER_EXIT_STRATEGY', 'TRAILING_STOP')
            
            # Paper-specific profit target
            new_profit_target = Prompt.ask(
                f"Paper Profit Target ({PaperConfig.PAPER_PROFIT_TARGET or Config.PROFIT_TARGET}%)", 
                default=str(PaperConfig.PAPER_PROFIT_TARGET or Config.PROFIT_TARGET)
            )
            if new_profit_target.strip():
                PaperConfig.update_param('PAPER_PROFIT_TARGET', float(new_profit_target))
                
            # Paper-specific stop loss
            new_stop_loss = Prompt.ask(
                f"Paper Stop Loss ({PaperConfig.PAPER_STOP_LOSS or Config.STOP_LOSS}%)", 
                default=str(PaperConfig.PAPER_STOP_LOSS or Config.STOP_LOSS)
            )
            if new_stop_loss.strip():
                PaperConfig.update_param('PAPER_STOP_LOSS', float(new_stop_loss))
                
            # Paper-specific trailing stop
            new_trailing_stop = Prompt.ask(
                f"Paper Trailing Stop ({PaperConfig.PAPER_TRAILING_STOP or Config.TRAILING_STOP}%)", 
                default=str(PaperConfig.PAPER_TRAILING_STOP or Config.TRAILING_STOP)
            )
            if new_trailing_stop.strip():
                PaperConfig.update_param('PAPER_TRAILING_STOP', float(new_trailing_stop))
                
            # Paper-specific max hold time
            new_max_hold_hours = Prompt.ask(
                f"Paper Max Hold Time ({(PaperConfig.PAPER_MAX_HOLD_TIME or Config.MAX_HOLD_TIME)/3600} hours)", 
                default=str((PaperConfig.PAPER_MAX_HOLD_TIME or Config.MAX_HOLD_TIME)/3600)
            )
            if new_max_hold_hours.strip():
                PaperConfig.update_param('PAPER_MAX_HOLD_TIME', float(new_max_hold_hours) * 3600)
        else:
            # Reset paper-specific settings to use main settings
            PaperConfig.update_param('PAPER_EXIT_STRATEGY', None)
            PaperConfig.update_param('PAPER_PROFIT_TARGET', None)
            PaperConfig.update_param('PAPER_STOP_LOSS', None)
            PaperConfig.update_param('PAPER_TRAILING_STOP', None)
            PaperConfig.update_param('PAPER_MAX_HOLD_TIME', None)
        
        PaperConfig.save_to_env()
        console.print("[bold green]Paper trading settings updated successfully![/bold green]")
    else:
        console.print("[cyan]Keeping current paper trading settings.[/cyan]")

# Function to configure exit strategy
async def configure_exit_strategy():
    """Configure exit strategy parameters with enhanced UI"""
    print_header("EXIT STRATEGY CONFIGURATION")
    
    # Create a panel with current exit strategy settings
    settings_panel = Panel.fit(
        f"• Strategy: [bold]{Config.EXIT_STRATEGY}[/bold]\n"
        f"• Profit Target: [bold green]{Config.PROFIT_TARGET}%[/bold green]\n"
        f"• Stop Loss: [bold red]{Config.STOP_LOSS}%[/bold red]\n"
        f"• Trailing Stop: [bold cyan]{Config.TRAILING_STOP}%[/bold cyan]\n"
        f"• Max Hold Time: [bold]{Config.MAX_HOLD_TIME/3600}[/bold] hours\n",
        title="[bold cyan]Current Exit Strategy Settings[/]", 
        border_style="cyan"
    )
    console.print(settings_panel)
    
    # Also show paper trading exit strategy settings if they differ
    if (PaperConfig.PAPER_ENABLED and
        (PaperConfig.PAPER_EXIT_STRATEGY is not None or
         PaperConfig.PAPER_PROFIT_TARGET is not None or
         PaperConfig.PAPER_STOP_LOSS is not None or
         PaperConfig.PAPER_TRAILING_STOP is not None or
         PaperConfig.PAPER_MAX_HOLD_TIME is not None)):
        
        paper_settings_panel = Panel.fit(
            f"• Paper Strategy: [bold]{PaperConfig.PAPER_EXIT_STRATEGY or Config.EXIT_STRATEGY}[/bold]\n"
            f"• Paper Profit Target: [bold green]{PaperConfig.PAPER_PROFIT_TARGET or Config.PROFIT_TARGET}%[/bold green]\n"
            f"• Paper Stop Loss: [bold red]{PaperConfig.PAPER_STOP_LOSS or Config.STOP_LOSS}%[/bold red]\n"
            f"• Paper Trailing Stop: [bold cyan]{PaperConfig.PAPER_TRAILING_STOP or Config.TRAILING_STOP}%[/bold cyan]\n"
            f"• Paper Max Hold Time: [bold]{(PaperConfig.PAPER_MAX_HOLD_TIME or Config.MAX_HOLD_TIME)/3600}[/bold] hours\n",
            title="[bold yellow]Current Paper Exit Strategy Settings[/]", 
            border_style="yellow"
        )
        console.print(paper_settings_panel)
    
    update_settings = Confirm.ask("Update exit strategy settings?", default=False)
    
    if update_settings:
        # Exit strategy settings
        console.print("\n[bold cyan]EXIT STRATEGY OPTIONS:[/bold cyan]")
        console.print("1. Full Sell at Take Profit (FULL_TP)")
        console.print("2. Trailing Stop Only (TRAILING_STOP)")
        console.print(f"Current: [bold]{Config.EXIT_STRATEGY}[/bold]")
        
        strategy_choice = Prompt.ask(
            "Select exit strategy", 
            choices=["1", "2"], 
            default="1" if Config.EXIT_STRATEGY == "FULL_TP" else "2"
        )
        
        if strategy_choice == "1": 
            Config.update_param('EXIT_STRATEGY', 'FULL_TP')
        elif strategy_choice == "2": 
            Config.update_param('EXIT_STRATEGY', 'TRAILING_STOP')
        
        # Additional trading parameters
        new_profit_target = Prompt.ask(
            f"Profit Target ({Config.PROFIT_TARGET}%)", 
            default=str(Config.PROFIT_TARGET)
        )
        if new_profit_target.strip(): 
            Config.update_param('PROFIT_TARGET', float(new_profit_target))
            
        new_stop_loss = Prompt.ask(
            f"Stop Loss ({Config.STOP_LOSS}%)", 
            default=str(Config.STOP_LOSS)
        )
        if new_stop_loss.strip(): 
            Config.update_param('STOP_LOSS', float(new_stop_loss))
            
        new_trailing_stop = Prompt.ask(
            f"Trailing Stop ({Config.TRAILING_STOP}%)", 
            default=str(Config.TRAILING_STOP)
        )
        if new_trailing_stop.strip(): 
            Config.update_param('TRAILING_STOP', float(new_trailing_stop))
        
        new_hold_hours = Prompt.ask(
            f"Max Hold Time ({Config.MAX_HOLD_TIME/3600} hours)", 
            default=str(Config.MAX_HOLD_TIME/3600)
        )
        if new_hold_hours.strip():
            Config.update_param('MAX_HOLD_TIME', float(new_hold_hours) * 3600)
            
        Config.save_to_env()
        console.print("[bold green]Exit strategy settings updated successfully![/bold green]")
        
        # Ask if user wants to apply same settings to paper trading
        if PaperConfig.PAPER_ENABLED:
            apply_to_paper = Confirm.ask("Apply these same settings to paper trading?", default=True)
            if apply_to_paper:
                PaperConfig.update_param('PAPER_EXIT_STRATEGY', None)
                PaperConfig.update_param('PAPER_PROFIT_TARGET', None)
                PaperConfig.update_param('PAPER_STOP_LOSS', None)
                PaperConfig.update_param('PAPER_TRAILING_STOP', None)
                PaperConfig.update_param('PAPER_MAX_HOLD_TIME', None)
                PaperConfig.save_to_env()
                console.print("[bold green]Applied settings to paper trading.[/bold green]")
    else:
        console.print("[cyan]Keeping current exit strategy settings.[/cyan]")

# Function to configure priority fees
async def configure_priority_fees():
    """Configure priority fee settings with enhanced UI"""
    print_header("PRIORITY FEE CONFIGURATION")
    
    # Create a panel with current priority fee settings
    fee_panel = Panel.fit(
        f"• Mode: [bold]{Config.PRIORITY_FEE_MODE}[/bold]\n" +
        (f"• Custom Fee: [bold]{Config.PRIORITY_FEE_LAMPORTS}[/bold] lamports ([bold]{Config.PRIORITY_FEE_LAMPORTS/1e9:.9f}[/bold] SOL)" 
         if Config.PRIORITY_FEE_MODE == 'custom' else ""),
        title="[bold cyan]Current Priority Fee Settings[/]", 
        border_style="cyan"
    )
    console.print(fee_panel)
    
    update_settings = Confirm.ask("Update priority fee settings?", default=False)
    
    if update_settings:
        # Priority fee settings
        console.print("\n[bold cyan]PRIORITY FEE OPTIONS:[/bold cyan]")
        console.print("1. Auto (Jupiter automatically adjusts)")
        console.print("2. Custom (Set a fixed fee)")
        
        priority_fee_choice = Prompt.ask(
            "Select priority fee mode", 
            choices=["1", "2"], 
            default="1" if Config.PRIORITY_FEE_MODE == "auto" else "2"
        )
        
        if priority_fee_choice == "1": 
            Config.update_param('PRIORITY_FEE_MODE', 'auto')
        elif priority_fee_choice == "2":
            Config.update_param('PRIORITY_FEE_MODE', 'custom')
            # Ask for custom fee amount
            new_fee = Prompt.ask(
                f"Enter fee in lamports", 
                default=str(Config.PRIORITY_FEE_LAMPORTS)
            )
            if new_fee.strip():
                Config.update_param('PRIORITY_FEE_LAMPORTS', int(new_fee))
        
        Config.save_to_env()
        console.print("[bold green]Priority fee settings updated successfully![/bold green]")
    else:
        console.print("[cyan]Keeping current priority fee settings.[/cyan]")

# Function to configure transaction settings
async def configure_transaction_settings():
    """Configure transaction settings with enhanced UI"""
    print_header("TRANSACTION SETTINGS CONFIGURATION")
    
    # Create a panel with current transaction settings
    transaction_panel = Panel.fit(
        f"• Slippage: [bold]{Config.DEFAULT_SLIPPAGE}%[/bold]\n"
        f"• Price Impact Warning: [bold yellow]{Config.PRICE_IMPACT_WARNING}%[/bold yellow]\n"
        f"• Price Impact Abort: [bold red]{Config.PRICE_IMPACT_ABORT}%[/bold red]\n",
        title="[bold cyan]Current Transaction Settings[/]", 
        border_style="cyan"
    )
    console.print(transaction_panel)
    
    update_settings = Confirm.ask("Update transaction settings?", default=False)
    
    if update_settings:
        new_slippage = Prompt.ask(
            f"Default Slippage ({Config.DEFAULT_SLIPPAGE}%)", 
            default=str(Config.DEFAULT_SLIPPAGE)
        )
        if new_slippage.strip():
            Config.update_param('DEFAULT_SLIPPAGE', float(new_slippage))
            
        new_impact_warning = Prompt.ask(
            f"Price Impact Warning ({Config.PRICE_IMPACT_WARNING}%)", 
            default=str(Config.PRICE_IMPACT_WARNING)
        )
        if new_impact_warning.strip():
            Config.update_param('PRICE_IMPACT_WARNING', float(new_impact_warning))
            
        new_impact_abort = Prompt.ask(
            f"Price Impact Abort ({Config.PRICE_IMPACT_ABORT}%)", 
            default=str(Config.PRICE_IMPACT_ABORT)
        )
        if new_impact_abort.strip():
            Config.update_param('PRICE_IMPACT_ABORT', float(new_impact_abort))
        
        Config.save_to_env()
        console.print("[bold green]Transaction settings updated successfully![/bold green]")
    else:
        console.print("[cyan]Keeping current transaction settings.[/cyan]")

# Function to manage Telegram chats
async def manage_telegram_chats():
    """Interface for managing monitored Telegram chats"""
    print_header("TELEGRAM CHAT MANAGEMENT")
    
    # Show current chats
    active_chats = get_active_chats()
    if active_chats:
        console.print("[bold]Currently monitored chats:[/]")
        for i, (chat_id, chat_name) in enumerate(active_chats):
            console.print(f"{i+1}. {chat_name} (ID: {chat_id})")
    else:
        console.print("[bold yellow]No chats currently being monitored.[/]")
    
    # Ask what action to perform
    action = Prompt.ask(
        "\nWhat would you like to do?",
        choices=["add", "remove", "replace", "back"],
        default="back"
    )
    
    if action == "add":
        new_chat_list = await add_new_chats(active_chats)
        if new_chat_list:
            await setup_telegram_handler(new_chat_list, Config.BUY_AMOUNT)
            console.print("[bold green]✓[/] Updated monitored chats!")
    
    elif action == "remove":
        if not active_chats:
            console.print("[bold yellow]No chats to remove.[/]")
            return
            
        remaining_chats = await remove_chats(active_chats)
        if remaining_chats:
            await setup_telegram_handler(remaining_chats, Config.BUY_AMOUNT)
            console.print("[bold green]✓[/] Updated monitored chats!")
        else:
            console.print("[bold yellow]Warning: All chats have been removed from monitoring.[/]")
    
    elif action == "replace":
        with console.status("[cyan]Setting up new chat monitoring...[/]"):
            new_chats = await fetch_channels()
            if new_chats:
                await setup_telegram_handler(new_chats, Config.BUY_AMOUNT)
                console.print("[bold green]✓[/] New chat monitoring setup complete!")

# Initial setup wizard
async def initial_setup():
    """Run initial setup for first-time users or when reconfiguring with beautiful UI"""
    print_header("SOLANA TRADING BOT SETUP WIZARD", "=")
    
    # Configure RPC
    with console.status("[bold green]Configuring RPC...[/]"):
        rpc_url = await configure_rpc()
    
    # Initialize Solana client
    with console.status("[bold green]Initializing Solana client...[/]"):
        solana_client = initialize_clients(rpc_url)
    
    # Configure wallet
    with console.status("[bold green]Configuring wallet...[/]"):
        payer_keypair = await configure_wallet()
        if not payer_keypair:
            return None, None, None, None
    
    # Configure Telegram
    with console.status("[bold green]Configuring Telegram...[/]"):
        telegram_client, chat_list = await configure_telegram()
        if not telegram_client or not chat_list:
            return None, None, None, None
    
    # Configure trading parameters
    with console.status("[bold green]Configuring trading parameters...[/]"):
        await configure_trading()
    
    # Configure paper trading
    with console.status("[bold green]Configuring paper trading...[/]"):
        await configure_paper_trading()
    
    # Configure exit strategy
    with console.status("[bold green]Configuring exit strategy...[/]"):
        await configure_exit_strategy()
    
    # Configure priority fees
    with console.status("[bold green]Configuring priority fees...[/]"):
        await configure_priority_fees()
    
    # Configure transaction settings
    with console.status("[bold green]Configuring transaction settings...[/]"):
        await configure_transaction_settings()
    
    # Show final configuration
    print_header("SETUP COMPLETE", "=")
    display_current_settings()
    
    confirm = Confirm.ask("\nAre these settings correct?", default=True)
    if not confirm:
        console.print("[bold yellow]Let's reconfigure the settings.[/bold yellow]")
        return await initial_setup()
    
    return solana_client, payer_keypair, telegram_client, chat_list

# Quick start with existing configuration
async def quick_start():
    """Start with existing configuration from .env file with beautiful UI"""
    print_header("QUICK START WITH EXISTING CONFIGURATION", "=")
    display_current_settings()
    
    confirm = Confirm.ask("\nUse these settings?", default=True)
    if not confirm:
        console.print("[bold yellow]Let's go through the setup wizard.[/bold yellow]")
        return await initial_setup()
    
    # Initialize Solana client with existing RPC
    console.print("[bold]Initializing Solana client...[/bold]")
    with console.status("[cyan]Connecting to Solana network...[/]"):
        solana_client = initialize_clients(Config.RPC)
    
    # Initialize wallet using existing wallet file or create new one
    try:
        # Check for existing wallet file
        if os.path.exists("wallet.dat"):
            # Try to load existing wallet
            console.print("[bold]Loading wallet...[/bold]")
            with console.status("[cyan]Loading existing wallet...[/]"):
                payer_keypair = initialize_wallet(None, use_existing=True)
            
            if payer_keypair:
                console.print(f"[bold green]✓[/] Loaded existing wallet: {payer_keypair.pubkey()}")
            else:
                console.print("\n[bold red]Could not load existing wallet.[/bold red]")
                private_key = Prompt.ask("Enter private key (or press Enter for new wallet)", password=True, default="")
                with console.status("[cyan]Initializing wallet...[/]"):
                    payer_keypair = initialize_wallet(private_key if private_key.strip() else None)
        else:
            # Create a new wallet
            console.print("[bold yellow]No existing wallet file found.[/bold yellow]")
            private_key = Prompt.ask("Enter private key (or press Enter for new wallet)", password=True, default="")
            with console.status("[cyan]Initializing wallet...[/]"):
                payer_keypair = initialize_wallet(private_key if private_key.strip() else None)
    except Exception as e:
        console.print(f"[bold red]Error initializing wallet: {e}[/bold red]")
        return None, None, None, None
    
    # Check SOL balance - Use direct call instead of async
    console.print("[bold]Checking balance...[/bold]")
    sol_balance = get_sol_balance()  # Now uses our new non-async version
    console.print(f"\nCurrent SOL balance: [bold green]{sol_balance}[/bold green] SOL")
    
    # Warn if balance is low
    if sol_balance < Config.BUY_AMOUNT:
        console.print(Panel.fit(
            f"Current: {sol_balance} SOL, Required for trading: {Config.BUY_AMOUNT} SOL",
            title="[bold red]LOW BALANCE WARNING[/]", 
            border_style="red"
        ))
        
        # Offer paper trading when balance is low
        if not PaperConfig.PAPER_ENABLED:
            use_paper = Confirm.ask("Would you like to enable paper trading to test without real funds?", default=True)
            if use_paper:
                PaperConfig.update_param('PAPER_ENABLED', True)
                console.print("[bold green]✓[/] Paper trading enabled!")
        
        proceed = Confirm.ask("Continue anyway?", default=False)
        if not proceed:
            console.print("[bold red]Exiting...[/bold red]")
            return None, None, None, None
    
    # Initialize Telegram client
    console.print("[bold]Connecting to Telegram...[/bold]")
    with console.status("[cyan]Initializing Telegram client...[/]"):
        telegram_client = await initialize_telegram()
    
    if not telegram_client:
        console.print("[bold red]Failed to initialize Telegram client.[/bold red]")
        return None, None, None, None
    
    # Fetch and select multiple groups
    with console.status("[cyan]Fetching available groups...[/]"):
        chat_list = await fetch_channels()
    
    if not chat_list:
        console.print("[bold red]Failed to set up Telegram monitoring.[/bold red]")
        return None, None, None, None
    
    return solana_client, payer_keypair, telegram_client, chat_list

# ================ ASYNC TRADING FUNCTIONS ================
async def buy_token_async(token_address, sol_amount=None, slippage=None):
    """Async version of buy_token to be called from async contexts"""
    # Import the enhanced version from trading module
    from trading import buy_token_async as enhanced_buy_token_async
    return await enhanced_buy_token_async(token_address, sol_amount, slippage, source="Manual")

async def sell_token_async(token_address, percentage=100, slippage=None):
    """Async version of sell_token to be called from async contexts"""
    # Import the enhanced version from trading module
    from trading import sell_token_async as enhanced_sell_token_async
    return await enhanced_sell_token_async(token_address, percentage, slippage)

async def wait_for_tokens_and_start_tracking_async(token_address, price_per_token, max_attempts=20, wait_time=3):
    """Async version of wait_for_tokens_and_start_tracking"""
    # Import the enhanced version from trading module
    from trading import wait_for_tokens_and_start_tracking_async as enhanced_wait_async
    return await enhanced_wait_async(token_address, price_per_token, max_attempts, wait_time)

# Function to handle trading mode toggle
async def toggle_trading_mode():
    """Toggle between real, paper, or both trading modes"""
    print_header("TRADING MODE SELECTION")
    
    # Create a panel showing current trading modes
    current_panel = Panel.fit(
        f"• Real Trading: [bold {'green' if Config.AUTO_TRADE else 'red'}]{'Enabled' if Config.AUTO_TRADE else 'Disabled'}[/]\n"
        f"• Paper Trading: [bold {'magenta' if PaperConfig.PAPER_ENABLED and PaperConfig.PAPER_AUTO_TRADE else 'red'}]{'Enabled' if PaperConfig.PAPER_ENABLED and PaperConfig.PAPER_AUTO_TRADE else 'Disabled'}[/]\n",
        title="[bold blue]Current Trading Modes[/]", 
        border_style="blue"
    )
    console.print(current_panel)
    
    # Create a menu for mode selection
    mode_panel = Panel.fit(
        "1. [green]Real Trading Only[/] (Paper Trading Disabled)\n"
        "2. [magenta]Paper Trading Only[/] (Real Trading Disabled)\n"
        "3. [cyan]Both Real & Paper Trading[/] (Parallel Execution)\n"
        "4. [yellow]Manual Trading Only[/] (All Auto-Trading Disabled)",
        title="[bold blue]SELECT TRADING MODE[/]", 
        border_style="blue"
    )
    console.print(mode_panel)
    
    mode_choice = Prompt.ask("Select trading mode", choices=["1", "2", "3", "4"], default="1")
    
    if mode_choice == "1":  # Real Trading Only
        Config.update_param('AUTO_TRADE', True)
        PaperConfig.update_param('PAPER_ENABLED', False)
        console.print("[bold green]✓[/] Real Trading Mode activated. Paper trading disabled.")
        
    elif mode_choice == "2":  # Paper Trading Only
        Config.update_param('AUTO_TRADE', False)
        PaperConfig.update_param('PAPER_ENABLED', True)
        PaperConfig.update_param('PAPER_AUTO_TRADE', True)
        console.print("[bold magenta]✓[/] Paper Trading Mode activated. Real trading disabled.")
        
    elif mode_choice == "3":  # Both Real & Paper
        Config.update_param('AUTO_TRADE', True)
        PaperConfig.update_param('PAPER_ENABLED', True)
        PaperConfig.update_param('PAPER_AUTO_TRADE', True)
        console.print("[bold cyan]✓[/] Parallel Trading Mode activated. Both real and paper trading enabled.")
        
    elif mode_choice == "4":  # Manual Only
        Config.update_param('AUTO_TRADE', False)
        PaperConfig.update_param('PAPER_AUTO_TRADE', False)
        console.print("[bold yellow]✓[/] Manual Trading Mode activated. All auto-trading disabled.")
    
    ## Save updated settings
    Config.save_to_env()
    PaperConfig.save_to_env()

# Interactive menu for bot control
async def interactive_menu():
    """Interactive menu for bot control with beautiful UI"""
    while not stop_event.is_set():
        try:
            # Create a menu panel with new options
            menu_panel = Panel.fit(
                "[bold cyan]GENERAL SETTINGS[/]\n"
                "1. [cyan]View Current Settings[/]\n"
                "2. [cyan]Change Buy Amount[/]\n"
                "3. [cyan]Change Profit Target[/]\n"
                "4. [cyan]Change Stop Loss[/]\n"
                "5. [cyan]Change Trailing Stop[/]\n"
                "6. [cyan]Change Exit Strategy[/]\n"
                "7. [cyan]Change Priority Fee Settings[/]\n"
                "8. [cyan]Configure All Parameters[/]\n"
                "9. [cyan]Configure Paper Trading[/]\n"
                "10. [cyan]Toggle Trading Mode[/]\n"
                "11. [cyan]Measure RPC Speed[/]\n\n"
                
                "[bold green]REAL TRADING[/]\n"
                "12. [green]Show Active Tracking[/]\n"
                "13. [green]Manual Buy Token[/]\n"
                "14. [yellow]Manual Sell Token[/]\n\n"
                
                "[bold magenta]PAPER TRADING[/]\n"
                "15. [magenta]Show Paper Portfolio[/]\n"
                "16. [magenta]Manual Paper Buy[/]\n"
                "17. [magenta]Manual Paper Sell[/]\n"
                "18. [magenta]Reset Paper Portfolio[/]\n\n"
                
                "[bold blue]TELEGRAM[/]\n"
                "19. [blue]Manage Telegram Chats[/]\n\n"
                
                "[bold purple]DATABASE[/]\n"
                "20. [purple]View Trade History[/]\n"
                "21. [purple]View Trading Statistics[/]\n\n"
                
                "0. [red]Exit Bot[/]",
                title="[bold blue]BOT CONTROL MENU[/]", 
                border_style="blue"
            )
            console.print(menu_panel)
            
            # Add paper trading indicator in the menu title if enabled
            if PaperConfig.PAPER_ENABLED:
                if Config.AUTO_TRADE and PaperConfig.PAPER_AUTO_TRADE:
                    mode_text = "[bold green]Real + Paper Trading Active[/]"
                elif Config.AUTO_TRADE:
                    mode_text = "[bold green]Real Trading Active[/] | Paper Trading Disabled"
                elif PaperConfig.PAPER_AUTO_TRADE:
                    mode_text = "Real Trading Disabled | [bold magenta]Paper Trading Active[/]"
                else:
                    mode_text = "Auto-Trading Disabled (Manual Only)"
                console.print(f"Current Mode: {mode_text}")
            
            choice = Prompt.ask("\nEnter your choice", 
                               choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                                        "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21"], 
                               default="1")
            
            if choice == "0":
                console.print("[bold red]Shutting down bot...[/bold red]")
                stop_event.set()
                break
            elif choice == "1":
                display_current_settings()
            elif choice == "2":
                change_buy_amount()
            elif choice == "3":
                change_profit_target()
            elif choice == "4":
                change_stop_loss()
            elif choice == "5":
                change_trailing_stop()
            elif choice == "6":
                change_exit_strategy()
            elif choice == "7":
                change_priority_fee_settings()
            elif choice == "8":
                configure_all_parameters()
            elif choice == "9":
                await configure_paper_trading()
            elif choice == "10":
                # Trading mode toggle
                await toggle_trading_mode()
            elif choice == "11":
                # Measure RPC speed
                console.print("[bold]Measuring RPC Speed...[/]")
                with console.status("[cyan]Testing RPC response time...[/]"):
                    delay_ms = measure_rpc_delay_sync()
                
                # Display results with colored indicators
                if delay_ms < 100:
                    speed_color = "green"
                    assessment = "Excellent"
                elif delay_ms < 200:
                    speed_color = "green"
                    assessment = "Good"
                elif delay_ms < 500:
                    speed_color = "yellow"
                    assessment = "Average"
                else:
                    speed_color = "red"
                    assessment = "Slow"
                
                results_panel = Panel.fit(
                    f"[bold cyan]RPC URL:[/] {Config.RPC}\n"
                    f"[bold cyan]Response Time:[/] [bold {speed_color}]{delay_ms:.2f} ms[/]\n"
                    f"[bold cyan]Performance Assessment:[/] [bold {speed_color}]{assessment}[/]\n",
                    title=f"[bold blue]RPC SPEED TEST RESULTS[/]", 
                    border_style="blue"
                )
                console.print(results_panel)
                
                # Offer to test backup RPC too
                if Confirm.ask("Would you like to test the backup RPC as well?", default=True):
                    # Temporarily swap RPC URLs
                    temp = Config.RPC
                    Config.RPC = Config.BACKUP_RPC
                    Config.BACKUP_RPC = temp
                    
                    with console.status("[cyan]Testing backup RPC response time...[/]"):
                        backup_delay_ms = measure_rpc_delay_sync()
                    
                    # Display backup results
                    if backup_delay_ms < 100:
                        backup_speed_color = "green"
                        backup_assessment = "Excellent"
                    elif backup_delay_ms < 200:
                        backup_speed_color = "green"
                        backup_assessment = "Good"
                    elif backup_delay_ms < 500:
                        backup_speed_color = "yellow"
                        backup_assessment = "Average"
                    else:
                        backup_speed_color = "red"
                        backup_assessment = "Slow"
                    
                    backup_results_panel = Panel.fit(
                        f"[bold cyan]Backup RPC URL:[/] {Config.RPC}\n"
                        f"[bold cyan]Response Time:[/] [bold {backup_speed_color}]{backup_delay_ms:.2f} ms[/]\n"
                        f"[bold cyan]Performance Assessment:[/] [bold {backup_speed_color}]{backup_assessment}[/]\n",
                        title=f"[bold blue]BACKUP RPC SPEED TEST RESULTS[/]", 
                        border_style="blue"
                    )
                    console.print(backup_results_panel)
                    
                    # Swap back
                    temp = Config.RPC
                    Config.RPC = Config.BACKUP_RPC
                    Config.BACKUP_RPC = temp
                    
                    # Offer to swap if backup is faster
                    if backup_delay_ms < delay_ms and (delay_ms - backup_delay_ms) > 50:  # At least 50ms improvement
                        if Confirm.ask(f"Backup RPC is {delay_ms - backup_delay_ms:.2f} ms faster. Would you like to make it your primary RPC?", default=True):
                            # Swap RPCs permanently
                            temp = Config.RPC
                            Config.RPC = Config.BACKUP_RPC
                            Config.BACKUP_RPC = temp
                            Config.save_to_env()
                            console.print("[bold green]✓[/] RPCs swapped successfully!")
            
            # REAL TRADING OPTIONS
            elif choice == "12":
                show_active_tracking()
            elif choice == "13":
                token_addr = Prompt.ask("Enter token address to buy")
                if token_addr.strip():
                    amount = float(Prompt.ask(f"Enter amount in SOL", default=str(Config.BUY_AMOUNT)))
                    with console.status("[bold green]Processing buy transaction...[/]"):
                        # Use the async version directly
                        success = await buy_token_async(token_addr, amount)
                        if success:
                            console.print("[bold green]Buy transaction submitted successfully![/bold green]")
                        else:
                            console.print("[bold red]Buy transaction failed. Check logs for details.[/bold red]")
            elif choice == "14":
                token_addr = Prompt.ask("Enter token address to sell")
                if token_addr.strip():
                    percentage = int(Prompt.ask("Enter percentage to sell (1-100)", default="100"))
                    with console.status("[bold yellow]Processing sell transaction...[/]"):
                        # Use the async version directly
                        success = await sell_token_async(token_addr, percentage)
                        if success:
                            console.print("[bold green]Sell transaction completed successfully![/bold green]")
                        else:
                            console.print("[bold red]Sell transaction failed. Check logs for details.[/bold red]")
            
            # PAPER TRADING OPTIONS
            elif choice == "15":
                # Show paper portfolio
                if not PaperConfig.PAPER_ENABLED:
                    console.print("[bold red]Paper trading is disabled. Enable it in settings first.[/bold red]")
                else:
                    show_paper_portfolio()
            elif choice == "16":
                # Manual paper buy
                if not PaperConfig.PAPER_ENABLED:
                    console.print("[bold red]Paper trading is disabled. Enable it in settings first.[/bold red]")
                else:
                    token_addr = Prompt.ask("Enter token address to paper buy")
                    if token_addr.strip():
                        amount = float(Prompt.ask(f"Enter amount in SOL", default=str(PaperConfig.PAPER_BUY_AMOUNT)))
                        with console.status("[bold magenta]Processing paper buy transaction...[/]"):
                            # Use the async version directly
                            success = await paper_buy_token_async(token_addr, amount)
                            if success:
                                console.print("[bold green]Paper buy transaction completed successfully![/bold green]")
                            else:
                                console.print("[bold red]Paper buy transaction failed. Check logs for details.[/bold red]")
            elif choice == "17":
                # Manual paper sell
                if not PaperConfig.PAPER_ENABLED:
                    console.print("[bold red]Paper trading is disabled. Enable it in settings first.[/bold red]")
                else:
                    # Show paper portfolio first
                    show_paper_portfolio()
                    
                    token_addr = Prompt.ask("Enter token address to paper sell")
                    if token_addr.strip():
                        if token_addr not in PaperConfig.paper_portfolio["tokens"]:
                            console.print(f"[bold red]Token {token_addr} not found in paper portfolio![/bold red]")
                        else:
                            percentage = int(Prompt.ask("Enter percentage to sell (1-100)", default="100"))
                            with console.status("[bold magenta]Processing paper sell transaction...[/]"):
                                # Use the async version directly
                                success = await paper_sell_token_async(token_addr, percentage)
                                if success:
                                    console.print("[bold green]Paper sell transaction completed successfully![/bold green]")
                                else:
                                    console.print("[bold red]Paper sell transaction failed. Check logs for details.[/bold red]")
            elif choice == "18":
                # Reset paper portfolio
                if not PaperConfig.PAPER_ENABLED:
                    console.print("[bold red]Paper trading is disabled. Enable it in settings first.[/bold red]")
                else:
                    reset_confirm = Confirm.ask(
                        "[bold yellow]⚠ This will reset your paper trading portfolio and all tokens.[/] Continue?",
                        default=False
                    )
                    
                    if reset_confirm:
                        # Ask for new starting balance
                        new_balance = float(Prompt.ask(
                            "Enter new starting SOL balance",
                            default=str(PaperConfig.PAPER_SOL_BALANCE)
                        ))
                        
                        # Reset the portfolio
                        PaperConfig.paper_portfolio = {
                            "sol_balance": new_balance,
                            "tokens": {},
                            "transaction_history": []
                        }
                        
                        # Clear any tracking data
                        PaperConfig.paper_price_tracking = {}
                        PaperConfig.paper_processed_addresses = set()
                        PaperConfig.paper_auto_sell_locks = {}
                        
                        # Update the SOL balance parameter too
                        PaperConfig.update_param('PAPER_SOL_BALANCE', new_balance)
                        
                        console.print(f"[bold green]✓[/] Paper portfolio reset with {new_balance} SOL balance")
            
            # TELEGRAM OPTIONS
            elif choice == "19":
                # Manage Telegram chats
                await manage_telegram_chats()
                
            # DATABASE OPTIONS
            elif choice == "20":
                # View trade history
                console.print("[bold]Recent Trade History[/]")
                
                # Get last 10 trades from database
                with db.lock:
                    conn = db.get_connection()
                    try:
                        cursor = conn.execute('''
                            SELECT token_address, buy_time, sell_time, roi_percent, exit_reason
                            FROM trades
                            ORDER BY buy_time DESC
                            LIMIT 10
                        ''')
                        
                        trades = cursor.fetchall()
                        
                        if trades:
                            history_table = Table(show_header=True, header_style="bold blue")
                            history_table.add_column("Token", style="cyan")
                            history_table.add_column("Buy Time", style="cyan")
                            history_table.add_column("Sell Time", style="cyan")
                            history_table.add_column("ROI", style="cyan")
                            history_table.add_column("Exit Reason", style="cyan")
                            
                            for trade in trades:
                                token, buy_time, sell_time, roi, reason = trade
                                roi_color = "green" if roi and roi > 0 else "red"
                                roi_str = f"[{roi_color}]{roi:.2f}%[/]" if roi else "Open"
                                
                                history_table.add_row(
                                    token[:16] + "...",
                                    buy_time[:19] if buy_time else "",
                                    sell_time[:19] if sell_time else "Open",
                                    roi_str,
                                    reason or "Still holding"
                                )
                            
                            console.print(history_table)
                        else:
                            console.print("[yellow]No trade history yet.[/]")
                    finally:
                        conn.close()
                        
            elif choice == "21":
                # View trading statistics
                stats = db.get_trade_statistics()
                
                if stats['closed_trades'] > 0:
                    stats_panel = Panel.fit(
                        f"[bold cyan]Total Trades:[/] {stats['total_trades']}\n"
                        f"[bold cyan]Closed Trades:[/] {stats['closed_trades']}\n"
                        f"[bold cyan]Winning Trades:[/] {stats['winning_trades']}\n"
                        f"[bold cyan]Losing Trades:[/] {stats['losing_trades']}\n"
                        f"[bold cyan]Win Rate:[/] {stats['win_rate']:.1f}%\n"
                        f"[bold cyan]Total P/L:[/] {stats['total_profit_loss']:.6f} SOL\n"
                        f"[bold cyan]Average ROI:[/] {stats['avg_roi']:.2f}%\n"
                        f"[bold cyan]Best Trade:[/] {stats['best_trade_roi']:.2f}%\n"
                        f"[bold cyan]Worst Trade:[/] {stats['worst_trade_roi']:.2f}%\n"
                        f"[bold cyan]Avg Hold Time:[/] {stats['avg_hold_time_hours']:.1f} hours\n",
                        title="[bold green]TRADING STATISTICS[/]",
                        border_style="green"
                    )
                    console.print(stats_panel)
                else:
                    console.print("[yellow]No completed trades yet. Statistics will appear after your first closed trade.[/]")
            
            # Small delay to prevent high CPU usage
            await asyncio.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error in menu: {e}")
            await asyncio.sleep(1)

async def main():
    """Main entry point for the trading bot with beautiful UI"""
    # Create a banner
    console.print("\n\n")
    console.print(Panel.fit(
        "[bold cyan]Monitors multiple Telegram groups for token addresses and trades automatically\n"
        "[bold cyan]Features optimized transaction execution with Jito MEV protection\n"
        "[bold cyan]Advanced exit strategies with trailing stop support\n"
        "[bold magenta]Now with Paper Trading for risk-free testing & practice![/]\n"
        "[bold yellow]Enhanced with liquidity checks, sentiment analysis & database tracking![/]",
        title="[bold green]ENHANCED SOLANA TRADING BOT[/]", 
        subtitle="[bold yellow]v2.0.0[/]",
        border_style="green",
        width=80,
        padding=(1, 2)
    ))
    console.print("\n")
    
    # Setup signal handlers for graceful shutdown
    setup_signal_handlers()
    
    # Initialize paper trading module
    initialize_paper_trading()
    
    # Initialize last message time
    Config.last_telegram_message_time = datetime.now()
    
    # Check if .env is properly configured (has RPC and API credentials)
    has_config = all([
        Config.RPC, 
        Config.TELEGRAM_API_ID, 
        Config.TELEGRAM_API_HASH, 
        Config.TELEGRAM_PHONE
    ])
    
    if has_config:
        # Offer quick start with existing configuration
        console.print(Panel.fit(
            "1. [bold green]Quick Start[/] with existing configuration\n"
            "2. [bold cyan]Run Setup Wizard[/] (reconfigure all settings)\n"
            "3. [bold magenta]Paper Trading Only[/] (disable real trading)",
            title="[bold blue]STARTUP OPTIONS[/]", 
            border_style="blue"
        ))
        
        choice = Prompt.ask("\nEnter your choice", choices=["1", "2", "3"], default="1")
        
        if choice == "1":
            solana_client, payer_keypair, telegram_client, chat_list = await quick_start()
        elif choice == "2":
            solana_client, payer_keypair, telegram_client, chat_list = await initial_setup()
        elif choice == "3":
            # Enable paper trading and disable real trading
            Config.update_param('AUTO_TRADE', False)
            PaperConfig.update_param('PAPER_ENABLED', True)
            PaperConfig.update_param('PAPER_AUTO_TRADE', True)
            Config.save_to_env()
            PaperConfig.save_to_env()
            
            console.print("[bold magenta]Paper Trading Mode Activated![/]")
            console.print("[bold yellow]Real trading disabled, paper trading enabled[/]")
            
            # Now run quick start
            solana_client, payer_keypair, telegram_client, chat_list = await quick_start()
    else:
        # No existing configuration, run initial setup
        console.print("\n[bold yellow]No complete configuration found. Running setup wizard...[/bold yellow]")
        solana_client, payer_keypair, telegram_client, chat_list = await initial_setup()
    
    # Check if setup was successful
    if not solana_client or not payer_keypair or not telegram_client or not chat_list:
        console.print(Panel.fit(
            "Please restart the application and check your configuration.",
            title="[bold red]SETUP FAILED[/]", 
            border_style="red"
        ))
        return
    
    # Start the handler with multiple chats
    console.print("[bold]Setting up Telegram monitoring...[/]")
    with console.status("[cyan]Connecting to Telegram groups...[/]"):
        await setup_telegram_handler(chat_list, Config.BUY_AMOUNT)
    
    # Start health monitoring
    console.print("[bold]Starting health monitoring...[/]")
    heartbeat_task = asyncio.create_task(heartbeat_monitor())
    
    # Show status with trading mode indicators
    trading_mode_text = ""
    if Config.AUTO_TRADE and PaperConfig.PAPER_ENABLED and PaperConfig.PAPER_AUTO_TRADE:
        trading_mode_text = "[bold green]Real Trading[/] + [bold magenta]Paper Trading[/] Active"
    elif Config.AUTO_TRADE:
        trading_mode_text = "[bold green]Real Trading Active[/]"
    elif PaperConfig.PAPER_ENABLED and PaperConfig.PAPER_AUTO_TRADE:
        trading_mode_text = "[bold magenta]Paper Trading Active[/]"
    else:
        trading_mode_text = "[bold yellow]Manual Trading Only[/]"
    
    # Create a chat list string for the status panel
    chats_info = "\n".join([f"[bold green]• {chat_name}[/]" for _, chat_name in chat_list])
    
    status_panel = Panel.fit(
        f"[bold green]Monitoring {len(chat_list)} chats:[/]\n{chats_info}\n\n"
        f"[bold]Trading Mode:[/] {trading_mode_text}\n\n"
        f"[bold]Real Trading Settings:[/]\n"
        f"• Buy Amount: {Config.BUY_AMOUNT} SOL per token\n"
        f"• Auto-Trade: {'Enabled' if Config.AUTO_TRADE else 'Disabled'}\n"
        f"• Auto-Sell: {'Enabled' if Config.AUTO_SELL else 'Disabled'}\n"
        f"• Exit Strategy: {Config.EXIT_STRATEGY}\n\n"
        
        f"[bold]Paper Trading Settings:[/]\n"
        f"• Paper Trading: {'Enabled' if PaperConfig.PAPER_ENABLED else 'Disabled'}\n"
        f"• Paper Balance: {PaperConfig.PAPER_SOL_BALANCE} SOL\n"
        f"• Paper Auto-Trade: {'Enabled' if PaperConfig.PAPER_AUTO_TRADE else 'Disabled'}\n"
        f"• Paper Auto-Sell: {'Enabled' if PaperConfig.PAPER_AUTO_SELL else 'Disabled'}\n\n"
        
        f"[bold]Safety Features:[/]\n"
        f"• Min Liquidity: ${Config.MIN_LIQUIDITY_USD}\n"
        f"• Honeypot Check: {'Enabled' if Config.CHECK_HONEYPOT else 'Disabled'}\n"
        f"• Sentiment Analysis: Enabled\n\n"
        
        f"[bold]RPC Performance:[/] {PaperConfig.avg_measured_delay:.2f} ms response time",
        title="[bold green]MONITORING STARTED[/]", 
        border_style="green"
    )
    console.print(status_panel)
    
    console.print("[bold cyan]Press Ctrl+C to stop the bot or use the menu to navigate[/]")
    
    try:
        # Run the interactive menu in the main task, not as a separate task
        menu_task = asyncio.create_task(interactive_menu())
        
        # Wait for stop_event to be set
        while not stop_event.is_set():
            await asyncio.sleep(1)
            
        # Cancel menu and heartbeat tasks when stop_event is set
        if not menu_task.done():
            menu_task.cancel()
            try:
                await menu_task
            except asyncio.CancelledError:
                pass
                
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
                
    except KeyboardInterrupt:
        console.print("[bold yellow]Bot stopped by user.[/bold yellow]")
        stop_event.set()
    finally:
        # Clean up
        if telegram_client and telegram_client.is_connected():
            console.print("[bold]Disconnecting from Telegram...[/bold]")
            with console.status("[cyan]Closing connection...[/]"):
                await telegram_client.disconnect()
        
        # Final status report
        console.print("[bold]Final status report:[/bold]")
        show_active_tracking()
        if PaperConfig.PAPER_ENABLED:
            console.print("[bold]Paper trading portfolio:[/bold]")
            show_paper_portfolio()
            
        console.print(Panel.fit(
            "Thank you for using Enhanced Solana Trading Bot",
            title="[bold green]BOT SHUTDOWN COMPLETE[/]", 
            border_style="green"
        ))

if __name__ == "__main__":
    # Run the main function
    asyncio.run(main())