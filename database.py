#!/usr/bin/env python3
"""
Database module for storing trade history and analytics
"""
import sqlite3
from datetime import datetime
import json
import threading
from typing import Dict, List, Optional, Tuple
from config import logger

class TradingDatabase:
    def __init__(self, db_path="trading_history.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.create_tables()
    
    def get_connection(self):
        """Get a new database connection"""
        return sqlite3.connect(self.db_path, check_same_thread=False)
    
    def create_tables(self):
        """Create necessary database tables"""
        with self.lock:
            conn = self.get_connection()
            try:
                # Trades table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_address TEXT NOT NULL,
                        buy_price REAL,
                        sell_price REAL,
                        buy_amount_sol REAL,
                        sell_amount_sol REAL,
                        buy_time TIMESTAMP,
                        sell_time TIMESTAMP,
                        profit_loss REAL,
                        roi_percent REAL,
                        telegram_source TEXT,
                        exit_reason TEXT,
                        max_roi_reached REAL,
                        hold_duration_seconds INTEGER,
                        price_impact_buy REAL,
                        price_impact_sell REAL,
                        status TEXT DEFAULT 'open'
                    )
                ''')
                
                # Price history table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS price_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_address TEXT NOT NULL,
                        price REAL,
                        timestamp TIMESTAMP,
                        volume_24h REAL
                    )
                ''')
                
                # Token metadata table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_metadata (
                        token_address TEXT PRIMARY KEY,
                        symbol TEXT,
                        name TEXT,
                        decimals INTEGER,
                        first_seen TIMESTAMP,
                        liquidity_usd REAL,
                        holder_count INTEGER,
                        is_honeypot BOOLEAN DEFAULT 0
                    )
                ''')
                
                # Create indexes for better performance
                conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_address)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_price_history_token ON price_history(token_address)')
                
                conn.commit()
            finally:
                conn.close()
    
    def record_buy(self, token_address: str, buy_price: float, buy_amount_sol: float, 
                   telegram_source: str, price_impact: float = 0.0) -> int:
        """Record a buy transaction"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.execute('''
                    INSERT INTO trades (
                        token_address, buy_price, buy_amount_sol, buy_time, 
                        telegram_source, price_impact_buy, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open')
                ''', (token_address, buy_price, buy_amount_sol, datetime.now(), 
                      telegram_source, price_impact))
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()
    
    def record_sell(self, token_address: str, sell_price: float, sell_amount_sol: float,
                    exit_reason: str, price_impact: float = 0.0):
        """Record a sell transaction and calculate metrics"""
        with self.lock:
            conn = self.get_connection()
            try:
                # Get the open trade for this token
                cursor = conn.execute('''
                    SELECT id, buy_price, buy_amount_sol, buy_time
                    FROM trades 
                    WHERE token_address = ? AND status = 'open'
                    ORDER BY buy_time DESC 
                    LIMIT 1
                ''', (token_address,))
                
                trade = cursor.fetchone()
                if not trade:
                    logger.warning(f"No open trade found for {token_address}")
                    return
                
                trade_id, buy_price, buy_amount_sol, buy_time = trade
                
                # Calculate metrics
                profit_loss = sell_amount_sol - buy_amount_sol
                roi_percent = ((sell_amount_sol - buy_amount_sol) / buy_amount_sol) * 100
                hold_duration = (datetime.now() - datetime.fromisoformat(buy_time)).total_seconds()
                
                # Update the trade record
                conn.execute('''
                    UPDATE trades SET 
                        sell_price = ?,
                        sell_amount_sol = ?,
                        sell_time = ?,
                        profit_loss = ?,
                        roi_percent = ?,
                        exit_reason = ?,
                        hold_duration_seconds = ?,
                        price_impact_sell = ?,
                        status = 'closed'
                    WHERE id = ?
                ''', (sell_price, sell_amount_sol, datetime.now(), profit_loss,
                      roi_percent, exit_reason, hold_duration, price_impact, trade_id))
                
                conn.commit()
                
                logger.info(f"Trade closed: {token_address} - ROI: {roi_percent:.2f}%")
            finally:
                conn.close()
    
    def update_max_roi(self, token_address: str, max_roi: float):
        """Update the maximum ROI reached for a trade"""
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute('''
                    UPDATE trades 
                    SET max_roi_reached = ?
                    WHERE token_address = ? AND status = 'open'
                ''', (max_roi, token_address))
                conn.commit()
            finally:
                conn.close()
    
    def record_price_point(self, token_address: str, price: float, volume_24h: float = 0):
        """Record a price data point"""
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute('''
                    INSERT INTO price_history (token_address, price, timestamp, volume_24h)
                    VALUES (?, ?, ?, ?)
                ''', (token_address, price, datetime.now(), volume_24h))
                conn.commit()
            finally:
                conn.close()
    
    def save_token_metadata(self, token_address: str, metadata: Dict):
        """Save token metadata"""
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute('''
                    INSERT OR REPLACE INTO token_metadata 
                    (token_address, symbol, name, decimals, first_seen, liquidity_usd, holder_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    token_address,
                    metadata.get('symbol', ''),
                    metadata.get('name', ''),
                    metadata.get('decimals', 9),
                    datetime.now(),
                    metadata.get('liquidity_usd', 0),
                    metadata.get('holder_count', 0)
                ))
                conn.commit()
            finally:
                conn.close()
    
    def get_trade_statistics(self) -> Dict:
        """Get overall trading statistics"""
        with self.lock:
            conn = self.get_connection()
            try:
                # Overall stats
                cursor = conn.execute('''
                    SELECT 
                        COUNT(*) as total_trades,
                        COUNT(CASE WHEN status = 'closed' THEN 1 END) as closed_trades,
                        COUNT(CASE WHEN profit_loss > 0 THEN 1 END) as winning_trades,
                        COUNT(CASE WHEN profit_loss < 0 THEN 1 END) as losing_trades,
                        SUM(profit_loss) as total_profit_loss,
                        AVG(roi_percent) as avg_roi,
                        MAX(roi_percent) as best_trade_roi,
                        MIN(roi_percent) as worst_trade_roi,
                        AVG(hold_duration_seconds) as avg_hold_time
                    FROM trades WHERE status = 'closed'
                ''')
                
                stats = cursor.fetchone()
                
                if stats[1] > 0:  # If we have closed trades
                    win_rate = (stats[2] / stats[1]) * 100 if stats[1] > 0 else 0
                    
                    return {
                        'total_trades': stats[0],
                        'closed_trades': stats[1],
                        'winning_trades': stats[2],
                        'losing_trades': stats[3],
                        'win_rate': win_rate,
                        'total_profit_loss': stats[4] or 0,
                        'avg_roi': stats[5] or 0,
                        'best_trade_roi': stats[6] or 0,
                        'worst_trade_roi': stats[7] or 0,
                        'avg_hold_time_hours': (stats[8] or 0) / 3600
                    }
                else:
                    return {
                        'total_trades': stats[0],
                        'closed_trades': 0,
                        'winning_trades': 0,
                        'losing_trades': 0,
                        'win_rate': 0,
                        'total_profit_loss': 0,
                        'avg_roi': 0,
                        'best_trade_roi': 0,
                        'worst_trade_roi': 0,
                        'avg_hold_time_hours': 0
                    }
            finally:
                conn.close()
    
    def get_token_history(self, token_address: str) -> List[Dict]:
        """Get all trades for a specific token"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.execute('''
                    SELECT * FROM trades 
                    WHERE token_address = ?
                    ORDER BY buy_time DESC
                ''', (token_address,))
                
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def mark_token_as_honeypot(self, token_address: str):
        """Mark a token as honeypot/scam"""
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute('''
                    UPDATE token_metadata 
                    SET is_honeypot = 1
                    WHERE token_address = ?
                ''', (token_address,))
                conn.commit()
            finally:
                conn.close()
    
    def is_known_honeypot(self, token_address: str) -> bool:
        """Check if token is marked as honeypot"""
        with self.lock:
            conn = self.get_connection()
            try:
                cursor = conn.execute('''
                    SELECT is_honeypot FROM token_metadata
                    WHERE token_address = ?
                ''', (token_address,))
                
                result = cursor.fetchone()
                return result[0] if result else False
            finally:
                conn.close()

# Global database instance
db = TradingDatabase()