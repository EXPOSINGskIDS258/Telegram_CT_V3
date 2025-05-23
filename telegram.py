#!/usr/bin/env python3
"""
Enhanced Telegram monitoring module with sentiment analysis and filtering
"""
import re
import threading
import asyncio
from datetime import datetime
from telethon import TelegramClient, events
from typing import Dict, List, Tuple, Optional

from config import Config, logger, extract_solana_addresses, stop_event
from trading import buy_token

# Global variables
telegram_client = None
active_chats = {}  # Dictionary to store chat_id -> chat_name mappings
message_handlers = {}  # Dictionary to store chat_id -> handler mappings

# Sentiment patterns
BULLISH_PATTERNS = [
    # Explicit buy signals
    r'(?i)\b(buy|buying|bought|aped|aped in|aping)\b',
    r'(?i)\b(gem|moon|moonshot|100x|1000x|rocket)\b',
    r'(?i)\b(bullish|pump|pumping|mooning)\b',
    r'(?i)\b(early|launching|launched|stealth)\b',
    
    # Emojis
    r'🚀{2,}|💎{2,}|🔥{2,}|📈{2,}|✅{2,}',
    r'🌙|🌕|🌛|🌜',
    
    # Call to action
    r'(?i)\b(get in|don\'t miss|last chance|now or never)\b',
    r'(?i)\b(low mc|low cap|micro cap)\b',
]

BEARISH_PATTERNS = [
    # Explicit sell signals
    r'(?i)\b(sell|sold|dump|dumped|dumping)\b',
    r'(?i)\b(rug|rugged|scam|honeypot|honey pot)\b',
    r'(?i)\b(exit|exited|out|bearish)\b',
    
    # Warnings
    r'(?i)\b(careful|warning|avoid|stay away)\b',
    r'(?i)\b(dead|dying|rekt|crashed)\b',
    
    # Negative emojis
    r'💩|📉|🚫|⚠️|🛑',
    r'(?i)\b(red|bleeding|crash)\b',
]

# Trusted sources (add known good callers)
TRUSTED_SOURCES = [
    # Add usernames or user IDs of trusted callers
    # Example: "trusted_caller_1", "whale_alerts", etc.
]

# Suspicious patterns that might indicate scam
SCAM_PATTERNS = [
    r'(?i)\b(guaranteed|risk free|100% safe)\b',
    r'(?i)\b(send sol to|airdrop|free tokens)\b',
    r'(?i)\b(limited time|act now|only \d+ spots)\b',
]

# Message quality score thresholds
MIN_MESSAGE_LENGTH = 10  # Minimum characters for a valid message
MAX_MESSAGE_LENGTH = 1000  # Maximum characters (avoid spam)
MIN_SENTIMENT_SCORE = 0.6  # Minimum confidence for trading

# ================ SENTIMENT ANALYSIS ================
def analyze_message_sentiment(message_text: str) -> Dict[str, any]:
    """
    Analyze message sentiment and quality for better filtering
    Returns sentiment analysis with confidence score
    """
    # Count pattern matches
    bullish_matches = 0
    bearish_matches = 0
    scam_matches = 0
    
    # Check bullish patterns
    for pattern in BULLISH_PATTERNS:
        if re.search(pattern, message_text):
            bullish_matches += 1
    
    # Check bearish patterns
    for pattern in BEARISH_PATTERNS:
        if re.search(pattern, message_text):
            bearish_matches += 1
    
    # Check scam patterns
    for pattern in SCAM_PATTERNS:
        if re.search(pattern, message_text):
            scam_matches += 1
    
    # Calculate sentiment
    total_patterns = bullish_matches + bearish_matches
    
    if total_patterns == 0:
        sentiment = 'neutral'
        confidence = 0.0
    elif bullish_matches > bearish_matches:
        sentiment = 'bullish'
        confidence = bullish_matches / (total_patterns + scam_matches)
    elif bearish_matches > bullish_matches:
        sentiment = 'bearish'
        confidence = bearish_matches / (total_patterns + scam_matches)
    else:
        sentiment = 'neutral'
        confidence = 0.5
    
    # Reduce confidence for scam patterns
    if scam_matches > 0:
        confidence *= (1 - (scam_matches * 0.3))  # 30% reduction per scam pattern
    
    # Message quality factors
    message_length = len(message_text)
    
    # Length penalty
    if message_length < MIN_MESSAGE_LENGTH:
        confidence *= 0.5
    elif message_length > MAX_MESSAGE_LENGTH:
        confidence *= 0.7
    
    # All caps penalty (often spam)
    if message_text.isupper() and len(message_text) > 20:
        confidence *= 0.6
    
    # Check for multiple addresses (often spam)
    addresses = extract_solana_addresses(message_text)
    if len(addresses) > 3:
        confidence *= 0.5
    
    return {
        'sentiment': sentiment,
        'confidence': min(max(confidence, 0.0), 1.0),  # Clamp between 0 and 1
        'bullish_signals': bullish_matches,
        'bearish_signals': bearish_matches,
        'scam_signals': scam_matches,
        'addresses_found': len(addresses),
        'message_length': message_length,
        'is_suspicious': scam_matches > 0 or confidence < 0.3
    }

def filter_token_addresses(addresses: List[str], message_analysis: Dict) -> List[str]:
    """
    Filter token addresses based on message analysis
    Returns filtered list of addresses worth investigating
    """
    if not addresses:
        return []
    
    # If message is too bearish or suspicious, skip all addresses
    if message_analysis['sentiment'] == 'bearish' or message_analysis['is_suspicious']:
        logger.warning(f"Skipping {len(addresses)} addresses due to bearish/suspicious message")
        return []
    
    # If confidence is too low, skip
    if message_analysis['confidence'] < MIN_SENTIMENT_SCORE:
        logger.warning(f"Skipping {len(addresses)} addresses due to low confidence: {message_analysis['confidence']:.2f}")
        return []
    
    # For neutral sentiment, only process if confidence is high
    if message_analysis['sentiment'] == 'neutral' and message_analysis['confidence'] < 0.7:
        logger.info(f"Skipping neutral message with low confidence")
        return []
    
    # Limit number of addresses to process from single message
    max_addresses = 2 if message_analysis['confidence'] > 0.8 else 1
    
    return addresses[:max_addresses]

def is_trusted_source(event) -> bool:
    """Check if message is from a trusted source"""
    try:
        sender = event.sender_id
        username = getattr(event.sender, 'username', None)
        
        # Check if sender is in trusted sources
        if sender in TRUSTED_SOURCES or username in TRUSTED_SOURCES:
            return True
    except:
        pass
    
    return False

# ================ ENHANCED MESSAGE HANDLER ================
async def create_message_handler(chat_id: int, chat_name: str, buy_amount: float):
    """Create an enhanced message handler for a specific chat"""
    async def handler_func(event):
        if stop_event.is_set():
            return
        
        try:
            message_text = event.message.text if event.message.text else "[No Text]"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Update last message time
            Config.last_telegram_message_time = datetime.now()
            
            # Remove emojis from message for Windows compatibility
            safe_message = re.sub(r'[^\x00-\x7F]+', '*', message_text)
            
            # Analyze message sentiment
            analysis = analyze_message_sentiment(message_text)
            
            # Log message with sentiment
            logger.info(f"New Message in {chat_name} at {timestamp}")
            logger.info(f"Message: {safe_message[:100]}...")  # First 100 chars
            logger.info(f"Sentiment: {analysis['sentiment']} (confidence: {analysis['confidence']:.2f})")
            
            # Check if from trusted source
            is_trusted = is_trusted_source(event)
            if is_trusted:
                logger.info("Message from TRUSTED source")
                analysis['confidence'] = min(analysis['confidence'] * 1.5, 1.0)  # Boost confidence
            
            # Extract potential Solana addresses
            addresses = extract_solana_addresses(message_text)
            
            if addresses:
                logger.info(f"Found {len(addresses)} potential token addresses")
                
                # Filter addresses based on sentiment
                filtered_addresses = filter_token_addresses(addresses, analysis)
                
                if filtered_addresses:
                    logger.info(f"Processing {len(filtered_addresses)} addresses after filtering")
                    
                    for address in filtered_addresses:
                        if address in Config.processed_addresses:
                            logger.info(f"Already processed address: {address}")
                            continue
                        
                        logger.info(f"New token address detected: {address} from {chat_name}")
                        logger.info(f"Message sentiment: {analysis['sentiment']} ({analysis['confidence']:.2f} confidence)")
                        
                        Config.processed_addresses.add(address)
                        
                        # Execute buy if auto-trading is enabled and sentiment is positive
                        if Config.AUTO_TRADE:
                            # Adjust buy amount based on confidence
                            adjusted_amount = buy_amount
                            if not is_trusted and analysis['confidence'] < 0.8:
                                adjusted_amount = buy_amount * 0.5  # Use half size for uncertain calls
                                logger.info(f"Using reduced position size due to confidence: {adjusted_amount} SOL")
                            
                            logger.info(f"Auto-trading enabled, buying token: {address}")
                            
                            # Pass source information to buy function
                            source_info = f"{chat_name} ({analysis['sentiment']})"
                            
                            # Run in a separate thread to avoid blocking
                            threading.Thread(
                                target=lambda: buy_token(address, adjusted_amount, Config.DEFAULT_SLIPPAGE, source_info)
                            ).start()
                        else:
                            logger.info(f"Token detected (auto-trading disabled): {address}")
                            logger.info(f"Sentiment: {analysis['sentiment']} - Consider manual review")
                else:
                    logger.info("No addresses passed sentiment filtering")
            else:
                # Log why no addresses were found
                if len(message_text) < MIN_MESSAGE_LENGTH:
                    logger.debug("Message too short")
                elif analysis['is_suspicious']:
                    logger.info("Message flagged as suspicious")
                else:
                    logger.debug("No token addresses found in message")
        
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    return handler_func

# ================ TELEGRAM MONITORING ================
async def initialize_telegram():
    """Initialize the Telegram client with credentials"""
    global telegram_client
    
    # Check if we have credentials
    if not all([Config.TELEGRAM_API_ID, Config.TELEGRAM_API_HASH, Config.TELEGRAM_PHONE]):
        logger.error("Telegram credentials not set. Check your .env file.")
        return None
    
    # Initialize client
    telegram_client = TelegramClient('session_name', 
                                    int(Config.TELEGRAM_API_ID), 
                                    Config.TELEGRAM_API_HASH)
    
    # Start the client
    await telegram_client.start(Config.TELEGRAM_PHONE)
    logger.info("Telegram client initialized")
    
    return telegram_client

async def fetch_channels():
    """Fetch and display all available groups and channels, allow selecting multiple."""
    if not telegram_client:
        logger.error("Telegram client not initialized")
        return None
    
    if not telegram_client.is_connected():
        await telegram_client.connect()
    
    logger.info("Fetching available groups and channels...")
    dialogs = await telegram_client.get_dialogs()

    groups = [dialog for dialog in dialogs if dialog.is_group or dialog.is_channel]

    if not groups:
        logger.error("No groups or channels found.")
        return None

    # Display available group chats
    for i, group in enumerate(groups):
        try:
            group_name = re.sub(r'[^\x00-\x7F]+', '*', group.name)
            logger.info(f"{i + 1}. {group_name} (ID: {group.id})")
        except Exception:
            logger.info(f"{i + 1}. Group {group.id}")

    # Ask how many chats to monitor
    while True:
        try:
            num_chats = int(input("\nHow many chats do you want to monitor? ").strip())
            if num_chats > 0:
                break
            else:
                logger.error("Please enter a number greater than 0.")
        except ValueError:
            logger.error("Please enter a valid number.")
    
    # Allow selecting multiple chats
    selected_chats = []
    
    logger.info(f"\nPlease select {num_chats} chats to monitor:")
    logger.info("TIP: Choose chats with quality alpha calls and active communities")
    
    for i in range(num_chats):
        while True:
            try:
                choice = int(input(f"Enter chat #{i+1} (1-{len(groups)}): ").strip()) - 1
                if 0 <= choice < len(groups):
                    selected_chat = groups[choice]
                    chat_name = re.sub(r'[^\x00-\x7F]+', '*', selected_chat.name)
                    
                    # Check if already selected
                    if selected_chat.id in [chat[0] for chat in selected_chats]:
                        logger.warning(f"Group '{chat_name}' already selected. Please choose another.")
                        continue
                        
                    selected_chats.append((selected_chat.id, chat_name))
                    logger.info(f"Added: {chat_name} (ID: {selected_chat.id})")
                    break
                else:
                    logger.error("Invalid selection. Please enter a valid number.")
            except ValueError:
                logger.error("Please enter a number.")
    
    if not selected_chats:
        logger.warning("No chats selected. Please select at least one chat.")
        return await fetch_channels()
        
    return selected_chats

async def setup_telegram_handler(chat_list, buy_amount=None):
    """Setup the enhanced Telegram message handler for multiple selected chats"""
    global telegram_client, message_handlers, active_chats
    
    if not telegram_client:
        logger.error("Telegram client not initialized")
        return False
    
    # Use config buy amount if not specified
    buy_amount = buy_amount or Config.BUY_AMOUNT
    
    # Clear existing handlers and active chats
    for handler in message_handlers.values():
        try:
            telegram_client.remove_event_handler(handler)
            logger.info(f"Removed handler")
        except Exception as e:
            logger.warning(f"Error removing existing handler: {e}")
    
    message_handlers = {}
    active_chats = {}
    
    # Setup handlers for each chat
    for chat_id, chat_name in chat_list:
        # Create enhanced handler
        handler_func = await create_message_handler(chat_id, chat_name, buy_amount)
        
        # Register the event handler
        new_message_event = events.NewMessage(chats=chat_id)
        telegram_client.add_event_handler(handler_func, new_message_event)
        
        # Store the handler reference
        message_handlers[chat_id] = handler_func
        active_chats[chat_id] = chat_name
        
        logger.info(f"Set up enhanced message handler for chat: {chat_name}")
    
    # Summary of active monitoring
    logger.info(f"Now monitoring {len(active_chats)} chats with sentiment analysis:")
    for chat_id, name in active_chats.items():
        logger.info(f"• {name} (ID: {chat_id})")
    
    logger.info("Sentiment filtering enabled - will analyze message quality before trading")
    
    return True

def get_active_chats():
    """Return a list of currently monitored chats"""
    return [(chat_id, name) for chat_id, name in active_chats.items()]

async def add_new_chats(existing_chats=None):
    """Add new chats to monitoring"""
    if existing_chats is None:
        existing_chats = get_active_chats()
    
    # Get current chat IDs
    existing_ids = [chat_id for chat_id, _ in existing_chats]
    
    # Fetch all available chats
    if not telegram_client:
        logger.error("Telegram client not initialized")
        return None
    
    if not telegram_client.is_connected():
        await telegram_client.connect()
    
    logger.info("Fetching available groups and channels...")
    dialogs = await telegram_client.get_dialogs()
    groups = [dialog for dialog in dialogs if dialog.is_group or dialog.is_channel]
    
    # Filter out already monitored chats
    available_groups = [(i, group) for i, group in enumerate(groups) 
                        if group.id not in existing_ids]
    
    if not available_groups:
        logger.info("No additional chats available to monitor.")
        return existing_chats
    
    # Display available group chats
    logger.info("\nAvailable chats to add:")
    for i, (_, group) in enumerate(available_groups):
        try:
            group_name = re.sub(r'[^\x00-\x7F]+', '*', group.name)
            logger.info(f"{i + 1}. {group_name} (ID: {group.id})")
        except Exception:
            logger.info(f"{i + 1}. Group {group.id}")
    
    # Ask how many additional chats to monitor
    while True:
        try:
            num_chats = int(input(f"\nHow many more chats do you want to add? ").strip())
            if num_chats > 0 and num_chats <= len(available_groups):
                break
            elif num_chats <= 0:
                logger.error("Please enter a number greater than 0.")
            else:
                logger.error(f"Please enter a number between 1 and {len(available_groups)}.")
        except ValueError:
            logger.error("Please enter a valid number.")
    
    # Select additional chats
    new_chats = []
    for i in range(num_chats):
        while True:
            try:
                choice = int(input(f"Enter chat #{i+1} (1-{len(available_groups)}): ").strip()) - 1
                if 0 <= choice < len(available_groups):
                    _, selected_chat = available_groups[choice]
                    available_groups.pop(choice)
                    
                    chat_name = re.sub(r'[^\x00-\x7F]+', '*', selected_chat.name)
                    new_chats.append((selected_chat.id, chat_name))
                    logger.info(f"Added: {chat_name} (ID: {selected_chat.id})")
                    
                    # Renumber the remaining options
                    if available_groups:
                        logger.info("\nRemaining chats:")
                        for j, (_, group) in enumerate(available_groups):
                            try:
                                group_name = re.sub(r'[^\x00-\x7F]+', '*', group.name)
                                logger.info(f"{j + 1}. {group_name} (ID: {group.id})")
                            except Exception:
                                logger.info(f"{j + 1}. Group {group.id}")
                    
                    break
                else:
                    logger.error("Invalid selection. Please enter a valid number.")
            except ValueError:
                logger.error("Please enter a number.")
            
            if not available_groups:
                break
        
        if not available_groups:
            if i < num_chats - 1:
                logger.info("No more chats available to add.")
            break
    
    # Combine existing and new chats
    combined_chats = existing_chats + new_chats
    return combined_chats

async def remove_chats(existing_chats=None):
    """Remove chats from monitoring"""
    if existing_chats is None:
        existing_chats = get_active_chats()
    
    if not existing_chats:
        logger.info("No chats currently being monitored.")
        return existing_chats
    
    # Display current chats
    logger.info("\nCurrently monitored chats:")
    for i, (_, chat_name) in enumerate(existing_chats):
        logger.info(f"{i + 1}. {chat_name}")
    
    # Ask which chats to remove
    logger.info("\nEnter the numbers of chats to remove (comma-separated), or 'all' to remove all:")
    choice = input("Chats to remove: ").strip().lower()
    
    if choice == 'all':
        logger.info("Removing all monitored chats.")
        return []
    
    if not choice:
        logger.info("No chats selected for removal.")
        return existing_chats
    
    try:
        # Parse indices to remove
        indices_to_remove = [int(idx.strip()) - 1 for idx in choice.split(',')]
        
        # Validate indices
        valid_indices = [idx for idx in indices_to_remove if 0 <= idx < len(existing_chats)]
        
        if not valid_indices:
            logger.error("No valid chat numbers provided. No changes made.")
            return existing_chats
        
        # Create new list excluding the selected indices
        remaining_chats = [chat for i, chat in enumerate(existing_chats) if i not in valid_indices]
        
        # Log removals
        for idx in valid_indices:
            _, chat_name = existing_chats[idx]
            logger.info(f"Removed: {chat_name}")
        
        return remaining_chats
    except ValueError:
        logger.error("Invalid input. Please use comma-separated numbers or 'all'.")
        return existing_chats

# Additional utility functions
def print_monitored_chats():
    """Print a list of all currently monitored chats"""
    chats = get_active_chats()
    
    if not chats:
        logger.info("No chats currently being monitored.")
        return
    
    logger.info("\nCurrently monitored chats:")
    for i, (chat_id, chat_name) in enumerate(chats):
        logger.info(f"{i + 1}. {chat_name} (ID: {chat_id})")

def analyze_chat_quality(chat_id: int, duration_hours: int = 24) -> Dict:
    """
    Analyze the quality of calls from a specific chat
    Returns statistics about the chat's performance
    """
    # This would analyze historical data from the database
    # For now, returning placeholder
    return {
        'total_calls': 0,
        'successful_calls': 0,
        'average_roi': 0.0,
        'sentiment_accuracy': 0.0
    }