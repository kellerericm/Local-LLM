"""Predict the sensor response y from the setting x."""


def predict(x: float) -> float:
    # First guess: a straight line.
    return 0.5 * x + 1.0
