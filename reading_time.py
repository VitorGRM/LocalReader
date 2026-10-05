"""Estimativa do tempo restante de leitura. Sem dependências de Qt.

Cada período começa com uma duração estimada pelo número de caracteres e por
uma velocidade de fala padrão (ajustada pelo controle de velocidade). Conforme
períodos são tocados, a duração real fica conhecida e a velocidade estimada dos
períodos restantes é recalculada a partir dela.
"""
import math

DEFAULT_CHARS_PER_SECOND = 15.0
# Peso do palpite inicial, em caracteres: com poucos períodos medidos a
# estimativa não oscila; com muitos, a velocidade real domina.
PRIOR_WEIGHT_CHARS = 200
MIN_SPEED_FACTOR = 0.5


def format_duration(milliseconds: float) -> str:
    seconds = math.ceil(max(milliseconds, 0) / 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class ReadingTimeEstimator:
    def __init__(self, gap_ms: int = 0):
        """``gap_ms``: pausa entre um período e o seguinte."""
        self.gap_ms = gap_ms
        self.reset([], 0)

    def reset(self, lengths: list[int], rate_percent: int) -> None:
        """Recomeça a estimativa (novo texto ou nova voz/velocidade)."""
        self._lengths = list(lengths)
        count = len(self._lengths)
        self._suffix = [0] * (count + 1)
        for i in range(count - 1, -1, -1):
            self._suffix[i] = self._suffix[i + 1] + self._lengths[i]
        self._known: dict[int, int] = {}
        self._measured_chars = 0
        self._measured_ms = 0
        speed = max(MIN_SPEED_FACTOR, 1 + rate_percent / 100)
        self._prior_cps = DEFAULT_CHARS_PER_SECOND * speed

    def record_duration(self, index: int, duration_ms: int) -> None:
        if not 0 <= index < len(self._lengths) or duration_ms <= 0:
            return
        previous = self._known.get(index)
        if previous is not None:
            self._measured_ms -= previous
            self._measured_chars -= self._lengths[index]
        self._known[index] = duration_ms
        self._measured_ms += duration_ms
        self._measured_chars += self._lengths[index]

    @property
    def chars_per_second(self) -> float:
        prior_seconds = PRIOR_WEIGHT_CHARS / self._prior_cps
        return (self._measured_chars + PRIOR_WEIGHT_CHARS) / (self._measured_ms / 1000 + prior_seconds)

    def remaining_ms(self, index: int, position_ms: int = 0) -> int:
        """Tempo até o fim, estando ``position_ms`` dentro do período ``index``."""
        count = len(self._lengths)
        if count == 0 or index < 0:
            return 0
        index = min(index, count - 1)
        cps = self.chars_per_second

        current = self._known.get(index)
        if current is None:
            current = self._lengths[index] / cps * 1000
        total = max(0.0, current - position_ms)

        # Períodos seguintes: duração real quando já conhecida, estimada nos demais.
        unknown_chars = self._suffix[index + 1]
        for later, duration in self._known.items():
            if later > index:
                total += duration
                unknown_chars -= self._lengths[later]
        total += unknown_chars / cps * 1000
        total += self.gap_ms * (count - 1 - index)
        return int(total)

    def total_ms(self) -> int:
        return self.remaining_ms(0, 0)
