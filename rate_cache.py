"""Arka planda döviz kurlarını güncelleyen thread-safe cache servisi."""
import threading
import logging
from datetime import datetime

logger = logging.getLogger('goba.cache')


class RateCache:
    """Thread-safe, arka planda periyodik güncellenen kur cache'i."""

    def __init__(self, fetcher_func, update_interval=10):
        self._fetcher = fetcher_func
        self._interval = update_interval
        self._rates: list = []
        self._ticker: list = []
        self._last_update: datetime | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        """Arka plan güncelleme thread'ini başlatır."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._updater_loop, daemon=True)
        self._thread.start()
        logger.info(f"RateCache started (interval={self._interval}s)")

        # İlk veriyi hemen çek
        self._fetch_now()

    def stop(self):
        """Thread'i durdurur."""
        self._running = False

    def _fetch_now(self):
        """Senkron veri çekme."""
        try:
            rates, ticker = self._fetcher()
            with self._lock:
                self._rates = rates
                self._ticker = ticker
                self._last_update = datetime.now()
        except Exception as e:
            logger.error(f"Rate fetch failed: {e}")

    def _updater_loop(self):
        """Arka plan güncelleme döngüsü."""
        while self._running:
            self._fetch_now()
            # interval kadar bekle, ama stop sinyalini de dinle
            for _ in range(self._interval):
                if not self._running:
                    break
                threading.Event().wait(1)

    def get_all(self):
        """Cache'lenmiş tüm veriyi döndür (rates, ticker, last_update)."""
        with self._lock:
            return (
                list(self._rates),
                list(self._ticker),
                self._last_update.strftime('%H:%M:%S') if self._last_update else '--:--:--'
            )

    @property
    def is_stale(self) -> bool:
        """Veri 2x interval süresinden eskiyse True."""
        if self._last_update is None:
            return True
        return (datetime.now() - self._last_update).total_seconds() > self._interval * 2
