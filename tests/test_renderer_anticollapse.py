"""Propiedad anti-colapso del renderizador de briefs.

Todo vector de ánimo *intencionado como estado distinto* debe renderizar un
brief distinto: el modelo solo puede percibir el ánimo a la resolución del
renderizador, así que dos estados colapsados son indistinguibles para él.

Hoy (base 653de09) el renderizador cuantiza valencia en 3 bandas (umbral
+-0.35) y energía en 3 bandas (0.35/0.7): solo ~9 estados efectivos. Estas
pruebas documentan el colapso (ROJO); la ampliación a ~6 bandas por eje
hará que pasen en verde.
"""

from __future__ import annotations

from harness.behavior import _render_brief

# Canales no explorados: valores medios representativos, fijos en toda la
# batería. Solo valencia/energía varían.
_MID = {
    "momentum": 0.0,
    "warmth": 0.6,
    "playfulness": 0.5,
    "reflectiveness": 0.5,
}


def _brief(*, valence: float, energy: float) -> str:
    return _render_brief(valence=valence, energy=energy, **_MID)


def test_valence_0_6_and_1_0_must_render_distinct_briefs() -> None:
    """La sonda de decisión original: valencia 0.6 vs 1.0 (energía igual).

    Hoy ambos caen en la banda '> 0.35' y rinden el mismo brief
    ('quietly bright') -> el modelo no puede distinguir el estado.
    """
    low = _brief(valence=0.6, energy=0.5)
    high = _brief(valence=1.0, energy=0.5)

    assert low != high


def test_energy_0_4_and_0_6_must_render_distinct_briefs() -> None:
    """Energía 0.4 vs 0.6 con todo lo demás idéntico.

    Ambos caen en la banda media (0.35 <= e <= 0.7) y rinden 'calmly present'
    -> colapso del canal de energía. (El par 0.4 vs 0.8 NO colapsa hoy: 0.8
    supera el umbral 0.7 y ya rinde 'lively and readily engaged'.)
    """
    low = _brief(valence=0.0, energy=0.4)
    high = _brief(valence=0.0, energy=0.6)

    assert low != high


def test_valence_energy_grid_must_render_injective_briefs() -> None:
    """Rejilla completa valencia x energía: 32 vectores -> 32 briefs.

    Con 8 valores de valencia y 4 de energía, la propiedad anti-colapso exige
    32 briefs distintos. Hoy el renderizador de 3x3 bandas colapsa la rejilla
    a 9 briefs -> ROJO.
    """
    valences = (-1.0, -0.7, -0.4, -0.1, 0.2, 0.5, 0.8, 1.0)
    energies = (0.1, 0.4, 0.7, 0.9)

    briefs = {_brief(valence=v, energy=e) for v in valences for e in energies}

    assert len(briefs) == len(valences) * len(energies)
