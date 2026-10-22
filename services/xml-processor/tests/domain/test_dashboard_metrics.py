"""Fórmulas del dashboard: casos borde (división por cero, periodos vacíos, límites de rango)."""

from datetime import date

import pytest
from app.domain.services.dashboard_metrics import (
    GREEN,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    NEUTRAL,
    RED,
    YELLOW,
    age_bucket_date_range,
    age_bucket_label,
    bucket_end,
    bucket_start,
    previous_period,
    safe_ratio_pct,
    traffic_light,
    trend,
    variation_pct,
)


class TestSafeRatio:
    def test_porcentaje_normal(self):
        assert safe_ratio_pct(1, 4) == 25.0

    def test_denominador_cero_es_none_no_cero(self):
        assert safe_ratio_pct(0, 0) is None
        assert safe_ratio_pct(5, 0) is None

    def test_numerador_cero_con_base_es_cero(self):
        assert safe_ratio_pct(0, 10) == 0.0

    def test_none_no_revienta(self):
        assert safe_ratio_pct(None, 10) is None
        assert safe_ratio_pct(1, None) is None


class TestVariation:
    def test_sube_y_baja(self):
        assert variation_pct(110, 100) == 10.0
        assert variation_pct(50, 100) == -50.0

    def test_anterior_cero_no_hay_base(self):
        assert variation_pct(10, 0) is None

    def test_sin_datos(self):
        assert variation_pct(None, 5) is None
        assert variation_pct(5, None) is None

    def test_actual_cero_es_menos_cien(self):
        assert variation_pct(0, 20) == -100.0


class TestPreviousPeriod:
    def test_misma_duracion_inmediatamente_anterior(self):
        assert previous_period(date(2026, 10, 1), date(2026, 10, 10)) == (
            date(2026, 9, 21),
            date(2026, 9, 30),
        )

    def test_un_solo_dia(self):
        assert previous_period(date(2026, 3, 1), date(2026, 3, 1)) == (
            date(2026, 2, 28),
            date(2026, 2, 28),
        )


class TestTrafficLight:
    @pytest.mark.parametrize(
        "value,expected", [(95, GREEN), (90, GREEN), (80, YELLOW), (75, YELLOW), (60, RED)]
    )
    def test_mayor_es_mejor(self, value, expected):
        assert traffic_light(value, HIGHER_IS_BETTER, 90, 75) == expected

    @pytest.mark.parametrize(
        "value,expected", [(1, GREEN), (2, GREEN), (4, YELLOW), (5, YELLOW), (8, RED)]
    )
    def test_menor_es_mejor(self, value, expected):
        assert traffic_light(value, LOWER_IS_BETTER, 2, 5) == expected

    def test_sin_alerta_solo_verde_o_rojo(self):
        assert traffic_light(80, HIGHER_IS_BETTER, 90) == RED

    def test_sin_meta_sin_valor_o_neutral_no_hay_semaforo(self):
        assert traffic_light(50, HIGHER_IS_BETTER, None) is None
        assert traffic_light(None, HIGHER_IS_BETTER, 90) is None
        assert traffic_light(50, NEUTRAL, 90) is None


class TestAgeBuckets:
    @pytest.mark.parametrize(
        "age,label",
        [
            (0, "0-3"),
            (3, "0-3"),
            (4, "4-7"),
            (7, "4-7"),
            (8, "8-15"),
            (15, "8-15"),
            (16, "15+"),
            (400, "15+"),
            (-2, "0-3"),
        ],
    )
    def test_limites(self, age, label):
        assert age_bucket_label(age) == label

    def test_rango_de_fechas_coincide_con_la_etiqueta(self):
        """El filtro por fechas y la etiqueta por edad deben coincidir en cada frontera."""
        today = date(2026, 10, 22)
        for age in range(0, 40):
            day = date.fromordinal(today.toordinal() - age)
            label = age_bucket_label(age)
            oldest, newest = age_bucket_date_range(label, today)
            assert (oldest is None or day >= oldest) and (newest is None or day <= newest)
            for other in ("0-3", "4-7", "8-15", "15+"):
                if other == label:
                    continue
                o, n = age_bucket_date_range(other, today)
                assert not ((o is None or day >= o) and (n is None or day <= n))

    def test_rango_desconocido(self):
        with pytest.raises(ValueError):
            age_bucket_date_range("99", date(2026, 1, 1))


class TestBuckets:
    def test_semana_empieza_lunes_y_termina_domingo(self):
        miercoles = date(2026, 10, 21)
        assert bucket_start(miercoles, "week") == date(2026, 10, 19)
        assert bucket_end(date(2026, 10, 19), "week") == date(2026, 10, 25)

    def test_mes_incluye_bisiesto_y_diciembre(self):
        assert bucket_end(date(2028, 2, 1), "month") == date(2028, 2, 29)
        assert bucket_end(date(2026, 12, 1), "month") == date(2026, 12, 31)

    def test_dia(self):
        assert bucket_start(date(2026, 1, 5), "day") == bucket_end(date(2026, 1, 5), "day")


class TestTrend:
    def test_sube_baja_estable(self):
        assert trend([10, 20]) == "up"
        assert trend([20, 10]) == "down"
        assert trend([100, 100.5]) == "flat"

    def test_sin_base(self):
        assert trend([]) is None
        assert trend([5]) is None
        assert trend([0, 5]) is None
