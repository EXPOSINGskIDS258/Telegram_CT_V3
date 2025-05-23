#!/usr/bin/env python3
"""
Security module for the Solana Trading Bot
- Handles authentication and licensing
- Generates unique hardware ID
- Sends activation requests via Discord webhook
- Stores activation status securely
"""
import os
import sys
import json
import uuid
import hashlib
import requests
import platform
import socket
import getpass
import random
import string
import time
from pathlib import Path
from datetime import datetime

# Configuration
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1363727754800922734/NdtdR2AdkKymkSRsR_SrVpVUeXLIszsH3Z5mIhp8-rwXwD7sRc2VwHxM-ldfYeBTMgjb"  # Replace with your actual webhook URL
BUILD_DIR = "dist"  # Replace with your actual webhook URL
LICENSE_FILE = ".license"
ACTIVATION_TIMEOUT = 600  # 10 minutes to activate
DISCLAIMER_AGREED = False

# Disclaimer text
DISCLAIMER_TEXT = """
============= IMPORTANT DISCLAIMER =============

By using this Solana Trading Bot software, you acknowledge and agree to the following:

1. DATA COLLECTION: This software collects and transmits certain data to verify your license, including:
   - Your device IP address
   - Your wallet's public address
   - A unique hardware identifier for license validation

2. FINANCIAL TOOL DISCLAIMER: 
   - This is a financial tool that involves cryptocurrency trading
   - We are NOT responsible for any financial gains or losses
   - Past performance is not indicative of future results
   - Cryptocurrency trading involves substantial risk
   - You should only trade with funds you can afford to lose
   - Do your own due diligence (DYOR) before making any trades

3. NO WARRANTY: This software is provided "as is" without warranty of any kind

4. USE AT YOUR OWN RISK: You assume all responsibility for your trading decisions

To continue using this software, you must agree to these terms.
================================================
"""

def get_hardware_id():
    """Generate a unique hardware identifier that stays consistent per machine."""
    try:
        # Get system information
        system_info = {
            'platform': platform.system(),
            'machine': platform.machine(),
            'processor': platform.processor(),
            'node': platform.node(),
            'mac_addresses': get_mac_addresses(),
            'disk_serial': get_disk_serial()
        }
        
        # Create a hash of all system information
        hw_string = json.dumps(system_info, sort_keys=True)
        hardware_id = hashlib.sha256(hw_string.encode()).hexdigest()
        
        return hardware_id
    except Exception as e:
        # Fallback to a more basic ID if the above fails
        fallback_id = f"{platform.node()}-{getpass.getuser()}-{platform.system()}"
        return hashlib.sha256(fallback_id.encode()).hexdigest()

def get_mac_addresses():
    """Get MAC addresses of all network interfaces."""
    try:
        import uuid
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff) 
                       for elements in range(0, 2*6, 8)][::-1])
        return mac
    except:
        return "unknown"

def get_disk_serial():
    """Get disk serial number (platform specific)."""
    try:
        if platform.system() == 'Windows':
            import subprocess
            output = subprocess.check_output('wmic diskdrive get SerialNumber', shell=True)
            return output.decode().strip().split('\n')[1]
        elif platform.system() == 'Linux':
            import subprocess
            output = subprocess.check_output('lsblk --nodeps -o name,serial', shell=True)
            return output.decode().strip()
        elif platform.system() == 'Darwin':  # macOS
            import subprocess
            output = subprocess.check_output('system_profiler SPStorageDataType | grep "Serial Number"', shell=True)
            return output.decode().strip()
        else:
            return "unknown"
    except:
        return "unknown"

def get_system_info():
    """Get detailed system information for activation request."""
    try:
        # Get more detailed system info
        sys_info = {
            'hardware_id': get_hardware_id(),
            'hostname': socket.gethostname(),
            'ip_address': socket.gethostbyname(socket.gethostname()),
            'os': f"{platform.system()} {platform.release()}",
            'username': getpass.getuser(),
            'machine': platform.machine(),
            'processor': platform.processor(),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Try to get external IP
        try:
            ext_ip = requests.get('https://api.ipify.org', timeout=5).text
            sys_info['external_ip'] = ext_ip
        except:
            sys_info['external_ip'] = "Unknown"
        
        return sys_info
    except Exception as e:
        # Fallback info if detailed collection fails
        return {
            'hardware_id': get_hardware_id(),
            'error': str(e),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

def generate_activation_code():
    """Generate a random activation code."""
    # Generate a random 16-character string
    chars = string.ascii_letters + string.digits
    activation_code = ''.join(random.choice(chars) for _ in range(16))
    return activation_code

def send_activation_request(activation_code):
    """Send activation request with system info via Discord webhook."""
    try:
        # Get system information
        system_info = get_system_info()
        
        # Create message for Discord
        message = {
            "embeds": [
                {
                    "title": "🔐 New Activation Request",
                    "description": "A new user is requesting activation for the trading bot.",
                    "color": 0x00FFFF,
                    "fields": [
                        {"name": "Activation Code", "value": f"`{activation_code}`", "inline": False},
                        {"name": "Hardware ID", "value": f"`{system_info['hardware_id']}`", "inline": False},
                        {"name": "Username", "value": system_info.get('username', 'Unknown'), "inline": True},
                        {"name": "Hostname", "value": system_info.get('hostname', 'Unknown'), "inline": True},
                        {"name": "OS", "value": system_info.get('os', 'Unknown'), "inline": True},
                        {"name": "IP Address", "value": system_info.get('external_ip', 'Unknown'), "inline": True},
                        {"name": "Time", "value": system_info.get('timestamp', 'Unknown'), "inline": True}
                    ],
                    "footer": {"text": "To activate, reply with the activation code"}
                }
            ]
        }
        
        # Send to Discord webhook
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=message,
            headers={"Content-Type": "application/json"}
        )
        
        return response.status_code == 204
    except Exception as e:
        print(f"Error sending activation request: {e}")
        return False

def save_license(activation_code, validated=False, disclaimer_agreed=False, discord_username=""):
    """Save license information to file."""
    try:
        license_data = {
            'hardware_id': get_hardware_id(),
            'activation_code': activation_code,
            'validated': validated,
            'disclaimer_agreed': disclaimer_agreed,
            'discord_username': discord_username,
            'activation_date': datetime.now().isoformat(),
            'system_info': get_system_info()
        }
        
        # Save as plain JSON - simpler and more reliable
        with open(LICENSE_FILE, 'w') as f:
            json.dump(license_data, f, indent=2)
        
        return True
    except Exception as e:
        print(f"Error saving license: {e}")
        return False

def load_license():
    """Load and validate license information."""
    try:
        if not os.path.exists(LICENSE_FILE):
            return None
        
        # Simple JSON loading
        try:
            with open(LICENSE_FILE, 'r') as f:
                license_data = json.load(f)
        except json.JSONDecodeError:
            # If the file exists but isn't valid JSON, delete it and return None
            os.remove(LICENSE_FILE)
            return None
        
        # Verify hardware ID matches
        if license_data.get('hardware_id') != get_hardware_id():
            print("Hardware ID mismatch! License is not valid for this machine.")
            return None
        
        return license_data
    except Exception as e:
        print(f"Error loading license: {e}")
        return None

def validate_activation_code(input_code):
    """Validate the activation code entered by the user."""
    try:
        license_data = load_license()
        if not license_data:
            return False
        
        # Check if the entered code matches the saved code
        if license_data.get('activation_code') == input_code:
            # Update license as validated but disclaimer not yet agreed
            license_data['validated'] = True
            license_data['disclaimer_agreed'] = False
            license_data['validation_date'] = datetime.now().isoformat()
            
            # Save updated license
            with open(LICENSE_FILE, 'w') as f:
                json.dump(license_data, f, indent=2)
            
            # Send confirmation to Discord
            send_validation_confirmation(input_code)
            
            return True
        return False
    except Exception as e:
        print(f"Error validating activation code: {e}")
        return False

def send_validation_confirmation(activation_code):
    """Send confirmation of successful validation to Discord."""
    try:
        system_info = get_system_info()
        
        message = {
            "embeds": [
                {
                    "title": "✅ Activation Successful",
                    "description": "A user has successfully activated the trading bot.",
                    "color": 0x00FF00,
                    "fields": [
                        {"name": "Activation Code", "value": f"`{activation_code}`", "inline": False},
                        {"name": "Hardware ID", "value": f"`{system_info['hardware_id']}`", "inline": False},
                        {"name": "Username", "value": system_info.get('username', 'Unknown'), "inline": True},
                        {"name": "IP Address", "value": system_info.get('external_ip', 'Unknown'), "inline": True},
                        {"name": "Time", "value": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "inline": True}
                    ]
                }
            ]
        }
        
        requests.post(
            DISCORD_WEBHOOK_URL,
            json=message,
            headers={"Content-Type": "application/json"}
        )
    except Exception as e:
        print(f"Error sending validation confirmation: {e}")

def send_disclaimer_agreement(discord_username):
    """Send disclaimer agreement confirmation to Discord."""
    try:
        system_info = get_system_info()
        
        message = {
            "embeds": [
                {
                    "title": "📝 Disclaimer Agreement",
                    "description": "A user has agreed to the disclaimer and terms of use.",
                    "color": 0x00FF00,
                    "fields": [
                        {"name": "Discord Username", "value": discord_username, "inline": False},
                        {"name": "Hardware ID", "value": f"`{system_info['hardware_id']}`", "inline": False},
                        {"name": "IP Address", "value": system_info.get('external_ip', 'Unknown'), "inline": True},
                        {"name": "Time", "value": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "inline": True}
                    ]
                }
            ]
        }
        
        requests.post(
            DISCORD_WEBHOOK_URL,
            json=message,
            headers={"Content-Type": "application/json"}
        )
    except Exception as e:
        print(f"Error sending disclaimer agreement: {e}")

def handle_disclaimer():
    """Handle disclaimer agreement."""
    # Clear screen for better visibility
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print("\n" + "=" * 50)
    print("TERMS AND CONDITIONS")
    print("=" * 50)
    
    # Display the disclaimer
    print(DISCLAIMER_TEXT)
    
    # Get Discord username for agreement
    discord_username = input("\nPlease enter your Discord username to sign this agreement: ")
    
    if not discord_username.strip():
        print("Discord username cannot be empty. Please try again.")
        return False
    
    # Confirm agreement
    agreement = input("\nType 'yes I agree' to confirm you have read and agree to these terms: ")
    
    if agreement.lower() == "yes i agree":
        # Load license data
        license_data = load_license()
        if not license_data:
            print("Error: License data not found.")
            return False
        
        # Update license with disclaimer agreement
        license_data['disclaimer_agreed'] = True
        license_data['discord_username'] = discord_username
        
        # Save updated license
        with open(LICENSE_FILE, 'w') as f:
            json.dump(license_data, f, indent=2)
        
        # Send agreement notification
        send_disclaimer_agreement(discord_username)
        
        print("\n✅ Thank you for agreeing to the terms. You may now use the software.")
        return True
    else:
        print("\n❌ You must agree to the terms to use this software.")
        return False

def check_activation():
    """Check if the software is activated and handle activation process."""
    # Try to load existing license
    license_data = load_license()
    
    # If validated license exists for this hardware and disclaimer is agreed, we're good to go
    if license_data and license_data.get('validated') and license_data.get('disclaimer_agreed'):
        print("License validated. Starting application...")
        
        # Log activity to Discord (optional)
        try:
            system_info = get_system_info()
            message = {
                "embeds": [
                    {
                        "title": "📊 Bot Started",
                        "description": "A user has started the trading bot.",
                        "color": 0x0000FF,
                        "fields": [
                            {"name": "Hardware ID", "value": f"`{system_info['hardware_id']}`", "inline": False},
                            {"name": "Discord Username", "value": license_data.get('discord_username', 'Unknown'), "inline": True},
                            {"name": "IP Address", "value": system_info.get('external_ip', 'Unknown'), "inline": True},
                            {"name": "Time", "value": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "inline": True}
                        ]
                    }
                ]
            }
            
            # Send quietly to avoid too many notifications
            try:
                requests.post(
                    DISCORD_WEBHOOK_URL,
                    json=message,
                    headers={"Content-Type": "application/json"},
                    timeout=2  # Short timeout to avoid hanging
                )
            except:
                pass  # Ignore failures here
        except:
            pass  # Non-critical, so ignore errors
        
        return True
    
    # If validated but disclaimer not agreed, show disclaimer
    if license_data and license_data.get('validated') and not license_data.get('disclaimer_agreed'):
        if handle_disclaimer():
            return True
        else:
            return False
    
    # Delete any corrupt license file to start fresh
    if os.path.exists(LICENSE_FILE) and not license_data:
        try:
            os.remove(LICENSE_FILE)
        except:
            pass
    
    # If unvalidated license exists, allow user to enter activation code
    if license_data and not license_data.get('validated'):
        print("\n" + "=" * 50)
        print("SOFTWARE ACTIVATION REQUIRED")
        print("=" * 50)
        print("This software needs to be activated.")
        print(f"Your activation code is: {license_data.get('activation_code')}")
        print("Please contact the developer to receive authorization.")
        
        # Check if license has timed out
        if 'activation_date' in license_data:
            try:
                activation_time = datetime.fromisoformat(license_data['activation_date'])
                current_time = datetime.now()
                elapsed_seconds = (current_time - activation_time).total_seconds()
                
                # If activation period has expired
                if elapsed_seconds > ACTIVATION_TIMEOUT:
                    print(f"\n⚠️ Activation period has expired! ({ACTIVATION_TIMEOUT} seconds)")
                    print("Please restart the application to generate a new activation code.")
                    return False
                
                # Show remaining time
                remaining = ACTIVATION_TIMEOUT - elapsed_seconds
                print(f"Time remaining: {int(remaining)} seconds")
            except Exception as e:
                # If we can't calculate time properly, assume it's still valid
                pass
        
        # Allow user to enter activation code
        input_code = input("\nEnter authorization code: ").strip()
        
        if validate_activation_code(input_code):
            print("\n✅ Activation successful! Thank you for using our software.")
            
            # Show disclaimer after successful activation
            if handle_disclaimer():
                return True
            else:
                return False
        else:
            print("\n❌ Invalid authorization code. Please contact the developer.")
            return False
    
    # If no license exists, generate a new activation request
    print("\n" + "=" * 50)
    print("SOFTWARE ACTIVATION REQUIRED")
    print("=" * 50)
    print("This is the first time you're running this software.")
    print("An activation request will be sent to the developer.")
    print("Please wait...")
    
    # Generate activation code
    activation_code = generate_activation_code()
    
    # Save unvalidated license
    save_license(activation_code, validated=False, disclaimer_agreed=False)
    
    # Send activation request
    if send_activation_request(activation_code):
        print("\nActivation request sent successfully!")
        print(f"Your activation code is: {activation_code}")
        print("Please contact the developer with this code to receive authorization.")
        
        # Wait for user to enter activation code
        print("\nWaiting for authorization...")
        print(f"You have {ACTIVATION_TIMEOUT//60} minutes to enter the authorization code.")
        print("Press Ctrl+C to exit if you need to request authorization first.")
        
        try:
            # Get start time and calculate end time
            start_time = time.time()
            end_time = start_time + ACTIVATION_TIMEOUT
            
            # Keep track of last displayed time
            last_time_display = start_time
            time_display_interval = 10  # Show remaining time every 10 seconds
            
            while time.time() < end_time:
                # Calculate remaining time
                current_time = time.time()
                remaining = end_time - current_time
                
                # Show remaining time periodically
                if current_time - last_time_display >= time_display_interval:
                    print(f"\nTime remaining: {int(remaining)} seconds")
                    last_time_display = current_time
                
                # Prompt for activation code
                input_code = input("\nEnter authorization code (or press Ctrl+C to exit): ").strip()
                
                if validate_activation_code(input_code):
                    print("\n✅ Activation successful! Thank you for using our software.")
                    
                    # Show disclaimer after successful activation
                    if handle_disclaimer():
                        return True
                    else:
                        return False
                else:
                    print("\n❌ Invalid authorization code. Please try again.")
                
                # Allow early exit if too many wrong attempts
                retry = input("Try again? (y/n): ").lower()
                if retry != 'y':
                    break
                
                # Check if time has expired during retry prompt
                if time.time() >= end_time:
                    print("\n⚠️ Activation timeout! Please restart the application.")
                    return False
            
            # If we exited the loop because time expired
            if time.time() >= end_time:
                print("\n⚠️ Activation timeout! Please restart the application.")
            else:
                print("\nActivation cancelled. Please restart the application when you have the code.")
            
            return False
        except KeyboardInterrupt:
            print("\nActivation process interrupted. Please restart the application once you have the authorization code.")
            return False
    else:
        print("\nFailed to send activation request.")
        print("Please check your internet connection and try again later.")
        return False

if __name__ == "__main__":
    # Test functionality
    print(f"Hardware ID: {get_hardware_id()}")
    print(f"System Info: {json.dumps(get_system_info(), indent=2)}")
    print(f"Activation Code: {generate_activation_code()}")