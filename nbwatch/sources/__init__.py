from ..browser import Browser


class SourceError(Exception):
    """La fuente falló (estructura cambiada, 0 resultados, error HTTP). partial = lo que se llegó a leer."""

    def __init__(self, msg, partial=None):
        super().__init__(msg)
        self.partial = partial or []


class SourceBlocked(SourceError):
    """La fuente devolvió una página de anti-bot/captcha."""


class SourceContext:
    def __init__(self, cfg):
        self.cfg = cfg
        self._browser = None

    def browser(self) -> Browser:
        if self._browser is None:
            self._browser = Browser(self.cfg.browser_profile, headless=self.cfg.get("browser", {}).get("headless", False))
            self._browser.__enter__()
        return self._browser

    def close(self):
        if self._browser is not None:
            self._browser.__exit__(None, None, None)
            self._browser = None

    def queries(self, fmt="{}"):
        """[(query, es_mpn)]: búsquedas generales con el formato de la fuente + MPNs vigilados."""
        out = [(fmt.format(t), False) for t in self.cfg["search_terms"]]
        out += [(m, True) for m in self.cfg.get("watch_mpns", [])]
        return out


def get_sources():
    from . import hpstore, idealo, trovaprezzi, unieuro

    return {
        "trovaprezzi": trovaprezzi.fetch,
        "idealo": idealo.fetch,
        "unieuro": unieuro.fetch,
        "hpstore": hpstore.fetch,
    }
