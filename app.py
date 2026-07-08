import os
import sys
import logging
import secrets
import requests
import pandas as pd
import feedparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from prophet import Prophet
import plotly.graph_objects as go
import json

from config import Config
from rate_cache import RateCache

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(Config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('goba')

app = Flask(__name__)
app.config.from_object(Config)

# Veritabanı ve Auth Kurulumu
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = "Bu sayfayı görüntülemek için giriş yapmalısınız."

# ── Models ───────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    portfolios = db.relationship('PortfolioItem', backref='owner', lazy=True)

class PortfolioItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    purchase_price = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Uygulama başlatılırken veritabanını oluştur
with app.app_context():
    db.create_all()

# Supported Currencies Settings
SUPPORTED_CURRENCIES = {
    "USDTRY=X": {"name": "Dolar", "flag": "us", "symbol": "₺", "base": "USD", "quote": "TRY", "precision": 4},
    "EURTRY=X": {"name": "Euro", "flag": "eu", "symbol": "₺", "base": "EUR", "quote": "TRY", "precision": 4},
    "GBPTRY=X": {"name": "Sterlin", "flag": "gb", "symbol": "₺", "base": "GBP", "quote": "TRY", "precision": 4},
    "GC=F":     {"name": "Ons Altın", "flag": "gold_ons", "symbol": "$", "base": "XAU", "quote": "USD", "precision": 2},
    "SI=F":     {"name": "Ons Gümüş", "flag": "silver", "symbol": "$", "base": "XAG", "quote": "USD", "precision": 2},
    "XAUTRY=X": {"name": "Gram Altın", "flag": "gold_gram", "symbol": "₺", "base": "XAU", "quote": "TRY", "precision": 2},
}

# Türev Altın Ürünleri (Gram Altın üzerinden hesaplanır)
GOLD_DERIVED = [
    {"id": "ceyrek",     "name": "Çeyrek Altın",     "name_en": "Quarter Gold",    "multiplier": 1.75,  "icon_type": "ceyrek"},
    {"id": "yarim",      "name": "Yarım Altın",      "name_en": "Half Gold",       "multiplier": 3.51,  "icon_type": "yarim"},
    {"id": "tam",        "name": "Tam Altın",        "name_en": "Full Gold",       "multiplier": 7.02,  "icon_type": "tam"},
    {"id": "cumhuriyet", "name": "Cumhuriyet Altın", "name_en": "Republic Gold",   "multiplier": 7.216, "icon_type": "cumhuriyet"},
]

# Cache Settings
CACHE_DIR = Config.CACHE_DIR
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"Cache directory created: {CACHE_DIR}")

def get_current_data_all():
    """Tüm güncel kur verilerini Yahoo üzerinden çeker."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    data = []
    
    # Önce USD/TRY kurunu çek (gram altın hesaplaması için gerekli)
    usd_try_rate = 1.0
    try:
        url = "https://query2.finance.yahoo.com/v8/finance/chart/USDTRY=X?range=1d&interval=1m"
        res = requests.get(url, headers=headers, timeout=Config.YAHOO_TIMEOUT).json()
        usd_try_rate = res['chart']['result'][0]['meta']['regularMarketPrice']
    except Exception as e:
        logger.warning(f"USD/TRY rate fetch error: {e}")
    
    # Altın ons verisini bir kez çek (hem Ons hem Gram için)
    gold_usd = 0.0
    gold_prev_usd = 0.0
    try:
        gold_url = "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m"
        gold_res = requests.get(gold_url, headers=headers, timeout=Config.YAHOO_TIMEOUT).json()
        gold_meta = gold_res['chart']['result'][0]['meta']
        gold_usd = gold_meta['regularMarketPrice']
        gold_prev_usd = gold_meta.get('previousClose', gold_usd)
    except Exception as e:
        logger.warning(f"Gold (GC=F) fetch error: {e}")
    
    # Gümüş verisini bir kez çek
    silver_usd = 0.0
    silver_prev_usd = 0.0
    try:
        silver_url = "https://query2.finance.yahoo.com/v8/finance/chart/SI=F?range=1d&interval=1m"
        silver_res = requests.get(silver_url, headers=headers, timeout=Config.YAHOO_TIMEOUT).json()
        silver_meta = silver_res['chart']['result'][0]['meta']
        silver_usd = silver_meta['regularMarketPrice']
        silver_prev_usd = silver_meta.get('previousClose', silver_usd)
    except Exception as e:
        logger.warning(f"Silver (SI=F) fetch error: {e}")
    
    for symbol, info in SUPPORTED_CURRENCIES.items():
        precision = info['precision']
        try:
            if symbol == 'GC=F':
                # Ons Altın: Direkt USD cinsinden
                rate_val = gold_usd
                prev_val = gold_prev_usd
                change = rate_val - prev_val
                change_percent = (change / prev_val) * 100 if prev_val != 0 else 0
            elif symbol == 'SI=F':
                # Gümüş: Direkt USD cinsinden
                rate_val = silver_usd
                prev_val = silver_prev_usd
                change = rate_val - prev_val
                change_percent = (change / prev_val) * 100 if prev_val != 0 else 0
            elif symbol == 'XAUTRY=X':
                # Gram Altın: (Ons USD * USD/TRY) / 31.1035
                rate_val = (gold_usd * usd_try_rate) / 31.1035
                prev_val = (gold_prev_usd * usd_try_rate) / 31.1035
                change = rate_val - prev_val
                change_percent = (change / prev_val) * 100 if prev_val != 0 else 0
            else:
                # Normal döviz kurları
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m"
                res = requests.get(url, headers=headers, timeout=Config.YAHOO_TIMEOUT).json()
                meta = res['chart']['result'][0]['meta']
                rate_val = meta['regularMarketPrice']
                prev_close = meta.get('previousClose', rate_val)
                change = rate_val - prev_close
                change_percent = (change / prev_close) * 100 if prev_close != 0 else 0
        except Exception as e:
            logger.warning(f"Yahoo live rate error for {symbol}: {e}")
            rate_val = 0.0
            change = 0.0
            change_percent = 0.0
            
        data.append({
            "symbol": symbol,
            "base": info['base'],
            "quote": info['quote'],
            "name": info["name"],
            "rate": round(rate_val, precision),
            "change": round(change, precision),
            "change_percent": round(change_percent, 2),
            "flag": info["flag"],
            "currency_symbol": info["symbol"],
            "update_time": datetime.now().strftime('%H:%M:%S'),
            "precision": precision
        })
    return data

def train_and_forecast(currency_symbol, periods=None):
    """Borsa verilerini çekerek gelişmiş Prophet tahmini üretir."""
    if periods is None:
        periods = Config.FORECAST_PERIODS
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        if currency_symbol in ('GC=F', 'SI=F'):
            # Emtia: Direkt futures (USD) verisini kullan
            emtia_url = f"https://query2.finance.yahoo.com/v8/finance/chart/{currency_symbol}?range=5y&interval=1d"
            emtia_res = requests.get(emtia_url, headers=headers, timeout=Config.YAHOO_HISTORY_TIMEOUT).json()
            
            if 'chart' not in emtia_res or emtia_res['chart']['result'] is None:
                logger.warning(f"Yahoo API Error ({currency_symbol}): {emtia_res}")
                return None
            
            emtia_data = emtia_res['chart']['result'][0]
            emtia_ts = emtia_data['timestamp']
            emtia_closes = emtia_data['indicators']['quote'][0]['close']
            
            df = pd.DataFrame({
                'ds': [datetime.fromtimestamp(t) for t in emtia_ts],
                'y': emtia_closes
            }).dropna()
        elif currency_symbol == 'XAUTRY=X':
            # Gram Altın: GC=F (USD) ve USDTRY=X verilerini çek, (Ons * USDTRY) / 31.1035
            gold_url = "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?range=5y&interval=1d"
            usd_url = "https://query2.finance.yahoo.com/v8/finance/chart/USDTRY=X?range=5y&interval=1d"
            
            gold_res = requests.get(gold_url, headers=headers, timeout=Config.YAHOO_HISTORY_TIMEOUT).json()
            usd_res = requests.get(usd_url, headers=headers, timeout=Config.YAHOO_HISTORY_TIMEOUT).json()
            
            if 'chart' not in gold_res or gold_res['chart']['result'] is None:
                logger.warning(f"Yahoo API Error (GC=F): {gold_res}")
                return None
            if 'chart' not in usd_res or usd_res['chart']['result'] is None:
                logger.warning(f"Yahoo API Error (USDTRY): {usd_res}")
                return None
            
            gold_data = gold_res['chart']['result'][0]
            usd_data = usd_res['chart']['result'][0]
            
            gold_ts = gold_data['timestamp']
            gold_closes = gold_data['indicators']['quote'][0]['close']
            usd_ts = usd_data['timestamp']
            usd_closes = usd_data['indicators']['quote'][0]['close']
            
            df_gold = pd.DataFrame({
                'ds': [datetime.fromtimestamp(t).date() for t in gold_ts],
                'gold_usd': gold_closes
            }).dropna()
            df_usd = pd.DataFrame({
                'ds': [datetime.fromtimestamp(t).date() for t in usd_ts],
                'usd_try': usd_closes
            }).dropna()
            
            df = pd.merge(df_gold, df_usd, on='ds', how='inner')
            df['y'] = (df['gold_usd'] * df['usd_try']) / 31.1035
            df['ds'] = pd.to_datetime(df['ds'])
            df = df[['ds', 'y']].dropna()
        else:
            # Normal döviz kurları
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{currency_symbol}?range=5y&interval=1d"
            res = requests.get(url, headers=headers, timeout=Config.YAHOO_HISTORY_TIMEOUT).json()
            
            if 'chart' not in res or res['chart']['result'] is None:
                logger.warning(f"Yahoo API Error: {res}")
                return None
                
            result_data = res['chart']['result'][0]
            timestamps = result_data['timestamp']
            closes = result_data['indicators']['quote'][0]['close']
            
            df = pd.DataFrame({
                'ds': [datetime.fromtimestamp(t) for t in timestamps], 
                'y': closes
            }).dropna()
        
        if df.empty:
            return None
        
        # 2. Model Kurulumu
        from prophet.make_holidays import make_holidays_df
        current_year = datetime.now().year
        tr_holidays = make_holidays_df(year_list=[current_year + i for i in range(-5, 4)], country='TR')
        
        model = Prophet(
            growth='linear',
            holidays=tr_holidays,
            yearly_seasonality=True,
            weekly_seasonality=False, 
            daily_seasonality=False,
            # Stabil bir trend tahmini için daha da optimize edildi
            changepoint_prior_scale=0.015,
            # Güven aralığı genişliği %50'ye düşürülerek çok daha dar bir bant sağlandı
            interval_width=0.5
        )
        model.add_seasonality(name='monthly', period=30.5, fourier_order=5)
        
        # 3. Eğitim ve Tahmin
        model.fit(df)
        
        future = model.make_future_dataframe(periods=periods)
        forecast = model.predict(future)
        
        # Sadece gerekli sütunlar
        result = forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].copy()
        
        # 4. Kayıt ve Grafik
        safe_name = currency_symbol.replace('=', '')
        csv_path = os.path.join(CACHE_DIR, f"{safe_name}_forecast.csv")
        result.to_csv(csv_path, index=False)
        
        # Plotly Grafiği (Modern Stil) - İki dilde de oluşturuyoruz
        langs = {
            'tr': {'actual': 'Gerçek Veri', 'forecast': 'Trend Tahmini', 'range': 'Tahmin Aralığı'},
            'en': {'actual': 'Actual Data', 'forecast': 'Trend Forecast', 'range': 'Forecast Range'}
        }
        
        for lang_code, labels in langs.items():
            fig = go.Figure()
            
            # Geçmiş Veri (Last 1 year for cleaner plot)
            df_plot = df[df['ds'] > (datetime.now() - timedelta(days=365))]
            fig.add_trace(go.Scatter(
                x=df_plot['ds'], y=df_plot['y'],
                name=labels['actual'],
                line=dict(color='#3b82f6', width=2)
            ))
            
            # Tahmin Verisi
            fig.add_trace(go.Scatter(
                x=result['ds'], y=result['yhat'],
                name=labels['forecast'],
                line=dict(color='#10b981', width=3, dash='dash')
            ))
            
            # Güven Aralığı
            x_fill = result['ds'].tolist() + result['ds'].tolist()[::-1]
            y_fill = result['yhat_upper'].tolist() + result['yhat_lower'].tolist()[::-1]
            
            fig.add_trace(go.Scatter(
                x=x_fill,
                y=y_fill,
                fill='toself',
                fillcolor='rgba(16, 185, 129, 0.1)',
                line=dict(color='rgba(255,255,255,0)'),
                hoverinfo="skip",
                showlegend=False,
                name=labels['range']
            ))
            
            fig.update_layout(
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font=dict(color='#f8fafc', family='Inter'),
                xaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)', zeroline=False),
                yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)', zeroline=False),
                margin=dict(l=0, r=0, t=20, b=80),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.15,
                    xanchor="center",
                    x=0.5
                )
            )
            
            plot_path = os.path.join(CACHE_DIR, f"{safe_name}_plot_{lang_code}.html")
            fig.write_html(plot_path, full_html=False, include_plotlyjs='cdn')
        
        return result
    except Exception as e:
        logger.error(f"Forecast error for {currency_symbol}: {e}")
        return None

def get_ticker_data(forex_data):
    """Forex verisinden türev altın/gümüş ürünlerini hesaplayıp geniş ticker listesi oluşturur."""
    ticker = []
    gram_item = None
    silver_item = None
    usd_try_item = None

    # icon_type → emoji + renk eşlemesi (frontend'de kullanılır)
    _icon_map = {
        "USDTRY=X": "usd",
        "EURTRY=X": "eur",
        "GBPTRY=X": "gbp",
        "GC=F":     "gold_ons",
        "SI=F":     "silver",
        "XAUTRY=X": "gold_gram",
    }

    for item in forex_data:
        ticker.append({
            "id": item["symbol"].replace("=", ""),
            "name": item["name"],
            "rate": item["rate"],
            "change": item["change"],
            "change_percent": item["change_percent"],
            "precision": item["precision"],
            "currency_symbol": item["currency_symbol"],
            "icon_type": _icon_map.get(item["symbol"], "default"),
            "update_time": item["update_time"],
        })
        if item["symbol"] == "XAUTRY=X":
            gram_item = item
        if item["symbol"] == "SI=F":
            silver_item = item
        if item["symbol"] == "USDTRY=X":
            usd_try_item = item

    # Türev altın ürünlerini ekle
    if gram_item:
        for gold in GOLD_DERIVED:
            derived_rate = round(gram_item["rate"] * gold["multiplier"], 2)
            derived_change = round(gram_item["change"] * gold["multiplier"], 2)
            ticker.append({
                "id": gold["id"],
                "name": gold["name"],
                "name_en": gold["name_en"],
                "rate": derived_rate,
                "change": derived_change,
                "change_percent": gram_item["change_percent"],
                "precision": 2,
                "currency_symbol": "₺",
                "icon_type": gold["icon_type"],
                "update_time": gram_item["update_time"],
            })

    # Gram Gümüş
    if silver_item and usd_try_item:
        usd_try_rate = usd_try_item["rate"]
        gram_silver_rate = round((silver_item["rate"] * usd_try_rate) / 31.1035, 2)
        gram_silver_change = round((silver_item["change"] * usd_try_rate) / 31.1035, 2)
        ticker.append({
            "id": "gram_gumus",
            "name": "Gram Gümüş",
            "name_en": "Gram Silver",
            "rate": gram_silver_rate,
            "change": gram_silver_change,
            "change_percent": silver_item["change_percent"],
            "precision": 2,
            "currency_symbol": "₺",
            "icon_type": "gram_gumus",
            "update_time": silver_item["update_time"],
        })

    return ticker


# ── Haber Sistemi (RSS Feed) ─────────────────────────────────────────────────
# TTL tabanlı bellek içi önbellek
_NEWS_CACHE = {
    "articles": [],
    "last_updated": None,
    "ttl_seconds": 6 * 3600  # 6 saat
}

# Çoklu RSS kaynak listesi — biri çalışmazsa diğerine geçilir
# lang: 'tr' olanlar Türkçe, 'en' olanlar İngilizce kaynaklardır
NEWS_FEEDS = [
    # ─── Küresel İngilizce Kaynaklar ───
    {
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "source": "BBC Business",
        "lang": "en"
    },
    {
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "source": "CNBC Finance",
        "lang": "en"
    },
    {
        "url": "https://www.forexlive.com/feed/news",
        "source": "ForexLive",
        "lang": "en"
    },
    {
        "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
        "source": "MarketWatch",
        "lang": "en"
    },
    {
        "url": "https://www.investing.com/rss/news_301.rss",
        "source": "Investing.com EN",
        "lang": "en"
    },
    # ─── Türkçe / Türkiye Kaynakları ───
    {
        "url": "https://tr.investing.com/rss/news_301.rss",
        "source": "Investing.com TR",
        "lang": "tr"
    },
    {
        "url": "https://www.bloomberght.com/rss",
        "source": "Bloomberg HT",
        "lang": "tr"
    },
    {
        "url": "https://bigpara.hurriyet.com.tr/rss/",
        "source": "BigPara",
        "lang": "tr"
    },
    {
        "url": "https://www.milliyet.com.tr/rss/rssNew/ekonomiRss.xml",
        "source": "Milliyet Ekonomi",
        "lang": "tr"
    },
]


def _parse_date(entry) -> datetime:
    """RSS entry'sinden yayın tarihini parse eder. Başarısız olursa şimdiki zamanı döndürür."""
    for attr in ("published", "updated", "created"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                # timezone-aware yap
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
    return datetime.utcnow()


def _classify_tag(title: str, summary: str, feed_lang: str = "en") -> str:
    """Haber başlığına/özetine göre otomatik etiket atar."""
    text = (title + " " + summary).lower()
    
    tag = "Global"
    
    # Kripto (En Yüksek Öncelik)
    if any(k in text for k in ["bitcoin", "crypto", "kripto", "ethereum", "blockchain", "binance", "coin", "token"]):
        tag = "Kripto"
    # Emtia
    elif any(k in text for k in ["gold", "silver", "altın", "gümüş", "commodity", "emtia",
                                "petrol", "oil", "copper", "bakır", "platinum"]):
        tag = "Emtia"
    # Merkez Bankası
    elif any(k in text for k in ["fed", "ecb", "central bank", "merkez bankası", "tcmb",
                                "interest rate", "faiz", "monetary policy", "para politikası",
                                "rate cut", "rate hike", "boe", "bank of england"]):
        tag = "Merkez Bankası"
    # Piyasalar / Borsa
    elif any(k in text for k in ["stock", "market", "s&p", "s\u0026p", "nasdaq", "dow", "borsa", "hisse",
                                "equit", "index", "endeks", "ipo", "shares", "wall street"]):
        tag = "Piyasalar"
    # Ekonomi / Makro
    elif any(k in text for k in ["inflation", "enflasyon", "cpi", "gdp", "gsyih", "büyüme",
                                "recession", "durgunluk", "unemployment", "işsizlik",
                                "trade deficit", "cari açık", "budget", "bütçe"]):
        tag = "Ekonomi"
    # Döviz
    elif any(k in text for k in ["dollar", "euro", "sterling", "forex", "currency", "dolar",
                                "döviz", "exchange rate", "kur", "usd", "eur", "gbp",
                                "yen", "yuan", "lira", " tl", "try", "parite"]):
        tag = "Döviz"
    # Türkiye özeli
    elif any(k in text for k in ["turkey", "turkish", "türkiye", "istanbul",
                               "borsa istanbul", "bist", "hazine", "türk lirası"]):
        tag = "Türkiye"
    # Analiz
    elif any(k in text for k in ["analysis", "analiz", "report", "rapor", "forecast",
                                "tahmin", "outlook", "görünüm", "review"]):
        tag = "Analiz"

    if feed_lang == "en":
        en_map = {
            "Kripto": "Crypto",
            "Emtia": "Commodities",
            "Merkez Bankası": "Central Bank",
            "Piyasalar": "Markets",
            "Ekonomi": "Economy",
            "Döviz": "Forex",
            "Türkiye": "Turkey",
            "Analiz": "Analysis",
            "Global": "Global"
        }
        tag = en_map.get(tag, tag)
    
    return tag


def _tag_color(tag: str) -> str:
    """Etikete renk sınıfı atar."""
    up_tags = {"Emtia", "Commodities", "Türkiye", "Turkey", "Analiz", "Analysis", "Piyasalar", "Markets", "Kripto", "Crypto"}
    return "rate-up" if tag in up_tags else "rate-down"


def fetch_news(force_refresh: bool = False) -> list:
    """RSS feed'lerinden finans haberlerini çeker. Cache geçerliyse cache'den döner."""
    now_utc = datetime.utcnow()
    TR_OFFSET = timedelta(hours=3)  # UTC+3 (Türkiye)
    cache = _NEWS_CACHE

    # Cache geçerliyse döndür
    if (
        not force_refresh
        and cache["last_updated"]
        and cache["articles"]
        and (now_utc - cache["last_updated"]).total_seconds() < cache["ttl_seconds"]
    ):
        logger.debug("Returning news from cache.")
        return cache["articles"]

    logger.info("Fetching fresh news from RSS feeds...")
    one_week_ago = now_utc - timedelta(days=7)  # Son 7 gün
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GOBA-Invest-Bot/1.0; +https://goba-invest.onrender.com)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }

    all_articles = []
    seen_titles = set()

    months_tr = {
        1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
        5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
        9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
    }

    for feed_cfg in NEWS_FEEDS:
        feed_lang = feed_cfg.get("lang", "en")
        try:
            resp = requests.get(feed_cfg["url"], headers=headers, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            if feed.bozo and not feed.entries:
                logger.warning(f"Feed parse error for {feed_cfg['source']}: {feed.bozo_exception}")
                continue

            count = 0
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if not title or title in seen_titles:
                    continue

                summary_raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                # HTML tag'larını temizle
                import re
                summary = re.sub(r"<[^>]+>", "", summary_raw).strip()
                if len(summary) > 280:
                    summary = summary[:277] + "..."

                link = getattr(entry, "link", "#") or "#"
                pub_date_utc = _parse_date(entry)  # UTC

                # Son 7 gün filtresi (UTC bazında)
                if pub_date_utc < one_week_ago:
                    continue

                # Gösterim tarihi: UTC+3 (Türkiye saati)
                pub_date_tr = pub_date_utc + TR_OFFSET

                tag = _classify_tag(title, summary, feed_lang)
                tag_color = _tag_color(tag)

                date_tr = f"{pub_date_tr.day} {months_tr[pub_date_tr.month]} {pub_date_tr.year}, {pub_date_tr.strftime('%H:%M')}"
                date_en = pub_date_tr.strftime("%b %d, %Y %H:%M")
                date_iso = pub_date_tr.strftime("%Y-%m-%dT%H:%M:%S")

                all_articles.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": feed_cfg["source"],
                    "lang": feed_lang,
                    "tag": tag,
                    "tag_color": tag_color,
                    "date_tr": date_tr,
                    "date_en": date_en,
                    "date_iso": date_iso,
                    "pub_timestamp": pub_date_utc.timestamp()
                })
                seen_titles.add(title)
                count += 1

            logger.info(f"Fetched {count} articles from {feed_cfg['source']}")

        except Exception as e:
            logger.warning(f"Failed to fetch from {feed_cfg['source']}: {e}")
            continue

    # Haberleri tarihe (gün) göre grupla
    from collections import defaultdict
    by_date = defaultdict(list)
    for a in all_articles:
        day_str = a['date_iso'][:10]
        by_date[day_str].append(a)
    
    sorted_days = sorted(by_date.keys(), reverse=True)
    for day in sorted_days:
        by_date[day].sort(key=lambda x: x["pub_timestamp"], reverse=True)

    final_articles = []
    # Her günden sırayla 1'er 1'er haber çek (round-robin)
    # Böylece haberler haftanın 7 gününe yayılır
    while len(final_articles) < 100:
        added = False
        for day in sorted_days:
            if by_date[day] and len(final_articles) < 100:
                final_articles.append(by_date[day].pop(0))
                added = True
        if not added:
            break

    # Son olarak yine kronolojik sırala ki arayüzde mantıklı görünsün
    final_articles.sort(key=lambda x: x["pub_timestamp"], reverse=True)
    all_articles = final_articles

    if all_articles:
        cache["articles"] = all_articles
        cache["last_updated"] = now_utc
        logger.info(f"News cache updated: {len(all_articles)} articles.")
    else:
        logger.warning("No articles fetched from any feed. Keeping stale cache if available.")

    return cache["articles"]


# ── CSRF Koruma ──────────────────────────────────────────────────────────────
@app.before_request
def csrf_protect():
    """Her istekte CSRF token oluştur, POST isteklerinde doğrula."""
    if request.method == 'POST':
        token = session.get('_csrf_token')
        form_token = request.form.get('_csrf_token')
        if not token or not form_token or not secrets.compare_digest(token, form_token):
            logger.warning(f"CSRF validation failed from {request.remote_addr}")
            return jsonify({'error': 'CSRF validation failed'}), 403

@app.before_request
def set_csrf_token():
    """Session'da CSRF token yoksa oluştur."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    g.csrf_token = session['_csrf_token']

# ── Auth & Portfolio Routes ──────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('portfolio'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            flash('Bu kullanıcı adı zaten alınmış.', 'error')
            return redirect(url_for('register'))
            
        new_user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        return redirect(url_for('portfolio'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('portfolio'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('portfolio'))
            
        flash('Geçersiz kullanıcı adı veya şifre.', 'error')
        
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/portfolio')
@login_required
def portfolio():
    # Güncel kurları çek
    rates, _, update_time = rate_cache.get_all()
    rate_dict = {item['symbol']: item for item in rates}
    
    items = PortfolioItem.query.filter_by(user_id=current_user.id).all()
    
    portfolio_data = []
    total_value_try = 0.0
    
    for item in items:
        symbol = item.symbol
        current_rate = rate_dict.get(symbol, {}).get('rate', 0)
        
        # TL Değeri hesapla
        if current_rate > 0:
            value_try = item.amount * current_rate
        else:
            value_try = 0
            
        profit = value_try - (item.purchase_price * item.amount)
        profit_pct = (profit / (item.purchase_price * item.amount) * 100) if item.purchase_price > 0 else 0
        
        portfolio_data.append({
            'id': item.id,
            'symbol': symbol,
            'name': SUPPORTED_CURRENCIES.get(symbol, {}).get('name', symbol),
            'amount': item.amount,
            'purchase_price': item.purchase_price,
            'current_price': current_rate,
            'value_try': value_try,
            'profit': profit,
            'profit_pct': profit_pct
        })
        
        total_value_try += value_try
        
    return render_template('portfolio.html', 
                           items=portfolio_data, 
                           total_value=total_value_try,
                           supported=SUPPORTED_CURRENCIES,
                           update_time=update_time)

@app.route('/api/portfolio', methods=['POST', 'DELETE'])
@login_required
def api_portfolio():
    if request.method == 'POST':
        symbol = request.form.get('symbol')
        amount = float(request.form.get('amount', 0))
        purchase_price = float(request.form.get('purchase_price', 0))
        
        if symbol not in SUPPORTED_CURRENCIES or amount <= 0:
            return jsonify({'error': 'Geçersiz veri'}), 400
            
        item = PortfolioItem(
            symbol=symbol,
            amount=amount,
            purchase_price=purchase_price,
            user_id=current_user.id
        )
        db.session.add(item)
        db.session.commit()
        return jsonify({'success': True})
        
    elif request.method == 'DELETE':
        item_id = request.form.get('item_id')
        item = PortfolioItem.query.filter_by(id=item_id, user_id=current_user.id).first()
        if item:
            db.session.delete(item)
            db.session.commit()
            return jsonify({'success': True})
        return jsonify({'error': 'Bulunamadı'}), 404



@app.route('/')
def index():
    rates, ticker_data, update_time = rate_cache.get_all()
    return render_template('index.html', forex_data=rates, ticker_data=ticker_data, update_time=update_time)

@app.route('/currency/<symbol>')
def currency_page(symbol):
    if symbol not in SUPPORTED_CURRENCIES:
        return redirect(url_for('index'))
    
    info = SUPPORTED_CURRENCIES[symbol]
    safe_name = symbol.replace('=', '')
    csv_path = os.path.join(CACHE_DIR, f"{safe_name}_forecast.csv")
    
    plot_path = os.path.join(CACHE_DIR, f"{safe_name}_plot.html")
    data = None
    
    # Cache kontrolü: Hem CSV hem de Grafik dosyası var mı bak
    if os.path.exists(csv_path) and os.path.exists(plot_path):
        mtime = os.path.getmtime(csv_path)
        # 2 saatten yeniyse cache kullan (Config'den okunur)
        if datetime.now().timestamp() - mtime < Config.CACHE_TTL_HOURS * 3600:
            try:
                data = pd.read_csv(csv_path)
                data['ds'] = pd.to_datetime(data['ds'])
            except:
                data = None
            
    if data is None or not os.path.exists(os.path.join(CACHE_DIR, f"{safe_name}_plot_tr.html")):
        data = train_and_forecast(symbol)
        if data is None:
             # Eğer download başarısız olduysa ama dosya varsa yine de eski dosyayı kullan
             if os.path.exists(csv_path):
                 data = pd.read_csv(csv_path)
                 data['ds'] = pd.to_datetime(data['ds'])

    if data is None:
        return "Veri alınamadı, borsa sunucuları yanıt vermiyor olabilir. Lütfen daha sonra tekrar deneyiniz."

    # Güncel veri cache'ten al
    all_rates, _, update_time = rate_cache.get_all()
    current_item = next((item for item in all_rates if item['symbol'] == symbol), None)
    
    current_rate = current_item['rate'] if current_item else 0.0
    change_val = current_item['change'] if current_item else 0.0
    change_pct = current_item['change_percent'] if current_item else 0.0
    
    # Try logic to get rate
    try:
        current_rate = float(current_item['rate']) if current_item else float(data[data['ds'] <= datetime.now()]['yhat'].iloc[-1])
    except:
        current_rate = 0.0

    def get_forecast_val(days):
        target_date = datetime.now() + timedelta(days=days)
        # En yakın tahmini bul
        diffs = (data['ds'] - target_date).abs()
        idx = diffs.idxmin()
        val = float(data.loc[idx, 'yhat'])
        date_en = data.loc[idx, 'ds'].strftime('%B %Y')
        date_tr = date_en
        # Türkçe ay isimleri için basit bir haritalama (Locale ile uğraşmamak için)
        months = {
            "January": "Ocak", "February": "Şubat", "March": "Mart", "April": "Nisan",
            "May": "Mayıs", "June": "Haziran", "July": "Temmuz", "August": "Ağustos",
            "September": "Eylül", "October": "Ekim", "November": "Kasım", "December": "Aralık"
        }
        for en, tr in months.items():
            date_tr = date_tr.replace(en, tr)
            
        precision = info.get('precision', 4)
        change = ((val - current_rate) / current_rate) * 100 if current_rate != 0 else 0
        return {"val": round(val, precision), "change": round(change, 2), "date_tr": date_tr, "date_en": date_en}

    forecasts = {
        "1w": get_forecast_val(7),
        "1m": get_forecast_val(30),
        "6m": get_forecast_val(180),
        "1y": get_forecast_val(365),
        "2y": get_forecast_val(730)
    }

    # 2 Yıllık Aylık Tahmin Listesi (Her ayın 1'i veya en yakını)
    monthly_targets = pd.date_range(start=datetime.now(), periods=25, freq='MS')
    monthly_data = []
    for target in monthly_targets:
        # En yakın tarihi bul
        diffs = (data['ds'] - target).abs()
        idx = diffs.idxmin()
        row = data.loc[idx].to_dict()
        monthly_data.append(row)
    
    table_list = monthly_data

    plot_file_tr = f"cache/{safe_name}_plot_tr.html"
    plot_file_en = f"cache/{safe_name}_plot_en.html"
    plot_exists = os.path.exists(os.path.join(CACHE_DIR, f"{safe_name}_plot_tr.html"))

    precision = info.get('precision', 4)
    fmt_rate = f"{current_rate:.{precision}f}"
    fmt_change = f"{change_val:+.{precision}f}"
    fmt_pct = f"{change_pct:+.2f}"

    return render_template('currency.html', 
                         info=info, 
                         symbol=symbol,
                         current_rate=fmt_rate,
                         change_val=fmt_change,
                         change_pct=fmt_pct,
                         forecasts=forecasts,
                         table_list=table_list,
                         plot_file_tr=plot_file_tr,
                         plot_file_en=plot_file_en,
                         plot_exists=plot_exists,
                         precision=precision,
                         update_time=current_item['update_time'] if current_item else update_time)

@app.route('/api/rates')
def api_rates():
    rates, ticker, update_time = rate_cache.get_all()
    return jsonify({
        "rates": rates,
        "ticker": ticker,
        "last_update": update_time,
        "stale": rate_cache.is_stale
    })

@app.route('/news')
def news():
    articles = fetch_news()
    last_updated_utc = _NEWS_CACHE.get("last_updated")
    if last_updated_utc:
        last_updated_tr = last_updated_utc + timedelta(hours=3)
        last_updated_str = last_updated_tr.strftime("%d.%m.%Y %H:%M")
    else:
        last_updated_str = "-"
    return render_template('news.html', articles=articles, last_updated=last_updated_str, article_count=len(articles))


@app.route('/api/news')
def api_news():
    """Haberleri JSON olarak döndürür. force=1 parametresiyle cache bypass edilir."""
    force = request.args.get('force', '0') == '1'
    articles = fetch_news(force_refresh=force)
    last_updated_utc = _NEWS_CACHE.get("last_updated")
    if last_updated_utc:
        last_updated_tr = last_updated_utc + timedelta(hours=3)
        last_updated_iso = last_updated_tr.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        last_updated_iso = None
    return jsonify({
        "articles": articles,
        "count": len(articles),
        "last_updated": last_updated_iso,
        "cache_ttl_seconds": _NEWS_CACHE["ttl_seconds"]
    })

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        logger.info(f"Contact form submitted: {request.form.get('name')} - {request.form.get('subject')}")
        return render_template('contact.html', success=True, csrf_token=g.csrf_token)
    return render_template('contact.html', csrf_token=g.csrf_token)

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# ── Arka Plan Kur Güncelleyici ────────────────────────────────────────────────
def _fetch_all_rates():
    """RateCache için fetcher — (rates, ticker) tuple döndürür."""
    rates = get_current_data_all()
    ticker = get_ticker_data(rates)
    return rates, ticker

rate_cache = RateCache(
    fetcher_func=_fetch_all_rates,
    update_interval=Config.RATE_UPDATE_INTERVAL
)
rate_cache.start()

if __name__ == '__main__':
    logger.info(f"Starting GOBA INVEST on port {Config.PORT} (debug={Config.DEBUG})")
    app.run(debug=Config.DEBUG, port=Config.PORT)
