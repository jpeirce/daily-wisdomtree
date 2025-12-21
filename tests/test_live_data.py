import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
from datetime import datetime, timedelta
import sys
import os

# Add scripts to path so we can import
sys.path.append(os.path.join(os.getcwd(), 'scripts'))
from fetch_and_summarize import fetch_live_data

class TestLiveData(unittest.TestCase):

    @patch('yfinance.Ticker')
    def test_sp500_trend_logic_yesterday(self, mock_ticker):
        # Setup mock data: 60 trading days
        dates = pd.date_range(end=datetime.now() - timedelta(days=1), periods=60, freq='B')
        mock_hist = pd.DataFrame({
            'Close': [100.0] * 60
        }, index=dates)
        
        # Set a 5% gain over the last 21 days
        # current_idx = -1 (yesterday)
        # prior_idx = -1 - 21 = -22
        mock_hist.iloc[-1, 0] = 105.0 # Current
        mock_hist.iloc[-22, 0] = 100.0 # Prior
        
        mock_instance = MagicMock()
        mock_instance.history.return_value = mock_hist
        mock_ticker.return_value = mock_instance
        
        data = fetch_live_data()
        
        self.assertEqual(data['sp500_trend_status'], "Trending Up")
        self.assertEqual(data['sp500_1mo_change_pct'], 5.0)
        self.assertIn(dates[-1].strftime('%Y-%m-%d'), data['sp500_trend_audit'])
        self.assertIn(dates[-22].strftime('%Y-%m-%d'), data['sp500_trend_audit'])

    @patch('yfinance.Ticker')
    @patch('fetch_and_summarize.datetime')
    def test_sp500_trend_logic_today_exclusion(self, mock_datetime, mock_ticker):
        # Setup mock data: 60 trading days
        fixed_now = datetime(2025, 12, 19, 12, 0, 0) # A Friday
        mock_datetime.now.return_value = fixed_now
        
        dates = pd.date_range(end=fixed_now, periods=60, freq='B')
        mock_hist = pd.DataFrame({
            'Close': [100.0] * 60
        }, index=dates)
        
        # fixed_now is dates[-1]. So last_date == today_date will be True.
        # current_idx should be -2 (Dec 18)
        # prior_idx should be -2 - 21 = -23
        mock_hist.iloc[-2, 0] = 95.0 # Yesterday (Current for analysis)
        mock_hist.iloc[-23, 0] = 100.0 # Prior
        
        mock_instance = MagicMock()
        mock_instance.history.return_value = mock_hist
        mock_ticker.return_value = mock_instance
        
        data = fetch_live_data()
        
        self.assertEqual(data['sp500_trend_status'], "Trending Down")
        self.assertEqual(data['sp500_1mo_change_pct'], -5.0)
        self.assertIn(dates[-2].strftime('%Y-%m-%d'), data['sp500_trend_audit'])
        self.assertIn(dates[-23].strftime('%Y-%m-%d'), data['sp500_trend_audit'])

    @patch('yfinance.Ticker')
    def test_insufficient_data(self, mock_ticker):
        # Setup mock data: only 10 days
        dates = pd.date_range(end=datetime.now(), periods=10, freq='B')
        mock_hist = pd.DataFrame({
            'Close': [100.0] * 10
        }, index=dates)
        
        mock_instance = MagicMock()
        mock_instance.history.return_value = mock_hist
        mock_ticker.return_value = mock_instance
        
        data = fetch_live_data()
        
        self.assertEqual(data['sp500_trend_status'], "Unknown")
        self.assertEqual(data['sp500_trend_audit'], "Insufficient data")

    @patch('yfinance.Ticker')
    @patch('fetch_and_summarize.datetime')
    def test_stale_data(self, mock_datetime, mock_ticker):
        # Setup: Today is Monday, but data ends last Tuesday (6 days ago)
        fixed_now = datetime(2025, 12, 22, 12, 0, 0) # A Monday
        mock_datetime.now.return_value = fixed_now
        
        # Last data point is Dec 16 (Tuesday prior)
        last_data_date = datetime(2025, 12, 16, 16, 0, 0)
        dates = pd.date_range(end=last_data_date, periods=60, freq='B')
        mock_hist = pd.DataFrame({
            'Close': [100.0] * 60
        }, index=dates)
        
        mock_instance = MagicMock()
        mock_instance.history.return_value = mock_hist
        mock_ticker.return_value = mock_instance
        
        data = fetch_live_data()
        
        self.assertEqual(data['sp500_trend_status'], "Unknown")
        self.assertIn("Data Stale", data['sp500_trend_audit'])

if __name__ == '__main__':
    unittest.main()
