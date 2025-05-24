#!/usr/bin/env python3
"""
Script to automatically update imports from Jupiter/Jito to Raydium
"""
import os
import re
import shutil
from datetime import datetime

# Files to update
FILES_TO_UPDATE = [
    'trading.py',
    'paper_trading.py',
    'main.py',
    'jito.py'  # We'll also check this one
]

# Backup directory
BACKUP_DIR = f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

def create_backup():
    """Create backup of files before modifying"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    for file in FILES_TO_UPDATE:
        if os.path.exists(file):
            shutil.copy2(file, os.path.join(BACKUP_DIR, file))
            print(f"Backed up {file}")

def update_imports_in_file(filepath):
    """Update imports in a single file"""
    if not os.path.exists(filepath):
        print(f"File {filepath} not found, skipping...")
        return
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    
    # Patterns to replace
    replacements = [
        # From jupiter import
        (r'from jupiter import (.+)', r'from raydium import \1'),
        # From jito import (for execute_swap functions)
        (r'from jito import (.*?)(execute_swap|get_quote)(.*?)', r'from raydium import \1\2\3'),
        # Import jupiter
        (r'import jupiter\b', r'import raydium'),
        # Import jito (only if it has swap functions)
        (r'import jito\b', r'import raydium  # Note: Also using jito for MEV'),
        # jupiter.function() calls
        (r'jupiter\.execute_swap', r'raydium.execute_swap'),
        (r'jupiter\.get_quote', r'raydium.get_quote'),
        # jito.function() calls for swaps
        (r'jito\.execute_swap', r'raydium.execute_swap'),
        (r'jito\.get_quote', r'raydium.get_quote'),
    ]
    
    changes_made = False
    for pattern, replacement in replacements:
        new_content = re.sub(pattern, replacement, content)
        if new_content != content:
            changes_made = True
            content = new_content
    
    if changes_made:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✓ Updated imports in {filepath}")
    else:
        print(f"  No changes needed in {filepath}")

def create_compatibility_wrapper():
    """Create a jupiter.py wrapper for backward compatibility"""
    wrapper_content = '''#!/usr/bin/env python3
"""
Jupiter compatibility wrapper that redirects to Raydium
This allows existing code to work without changes
"""
from raydium import (
    get_quote,
    get_quote_async,
    execute_swap,
    execute_swap_async,
    get_swap_tx,
    get_swap_tx_async,
)

# Re-export all functions
__all__ = [
    'get_quote',
    'get_quote_async',
    'execute_swap',
    'execute_swap_async',
    'get_swap_tx',
    'get_swap_tx_async',
]

print("Note: Using Raydium V4 instead of Jupiter")
'''
    
    with open('jupiter.py', 'w') as f:
        f.write(wrapper_content)
    print("✓ Created jupiter.py compatibility wrapper")

def main():
    print("=== Jupiter to Raydium Migration Script ===\n")
    
    # Check if raydium.py exists
    if not os.path.exists('raydium.py'):
        print("ERROR: raydium.py not found!")
        print("Please save the Raydium module code as 'raydium.py' first.")
        return
    
    # Create backup
    print("Creating backup...")
    create_backup()
    print(f"Backup created in {BACKUP_DIR}/\n")
    
    # Update imports
    print("Updating imports...")
    for file in FILES_TO_UPDATE:
        update_imports_in_file(file)
    
    # Create compatibility wrapper
    print("\nCreating compatibility wrapper...")
    create_compatibility_wrapper()
    
    print("\n=== Migration Complete ===")
    print("\nNext steps:")
    print("1. Test with paper trading first")
    print("2. Monitor logs for any issues")
    print("3. If you encounter problems, restore from backup:")
    print(f"   cp {BACKUP_DIR}/*.py .")
    
    # Check for potential issues
    print("\n=== Checking for potential issues ===")
    
    # Check if SPL token library is installed
    try:
        import spl.token
        print("✓ SPL Token library is installed")
    except ImportError:
        print("✗ SPL Token library not found. Install with: pip install spl-token-py")
    
    # Check for any remaining jupiter imports
    remaining_issues = []
    for file in FILES_TO_UPDATE:
        if os.path.exists(file):
            with open(file, 'r') as f:
                content = f.read()
                if 'jupiter' in content.lower() and 'raydium' not in content:
                    remaining_issues.append(file)
    
    if remaining_issues:
        print(f"\n⚠ Files that might still reference Jupiter: {', '.join(remaining_issues)}")
        print("  Please check these files manually.")

if __name__ == "__main__":
    main()