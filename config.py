import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Uygulama konfigürasyonu — tüm değerler .env üzerinden yönetilir."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'goba-invest-dev-key-change-in-production')
    DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'
    PORT = int(os.environ.get('PORT', 5000))

    # Cache
    CACHE_DIR = os.environ.get('CACHE_DIR', 'static/cache')
    CACHE_TTL_HOURS = int(os.environ.get('CACHE_TTL_HOURS', '2'))

    # Yahoo Finance
    YAHOO_TIMEOUT = int(os.environ.get('YAHOO_TIMEOUT', '10'))
    YAHOO_HISTORY_TIMEOUT = int(os.environ.get('YAHOO_HISTORY_TIMEOUT', '20'))
    YAHOO_HISTORY_RANGE = os.environ.get('YAHOO_HISTORY_RANGE', '5y')

    # Forecast
    FORECAST_PERIODS = int(os.environ.get('FORECAST_PERIODS', '730'))
    RATE_UPDATE_INTERVAL = int(os.environ.get('RATE_UPDATE_INTERVAL', '10'))

    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = os.environ.get('LOG_FILE', 'app.log')
