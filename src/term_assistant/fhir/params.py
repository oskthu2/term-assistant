"""Hjälpfunktioner för FHIR Parameters-resurser."""

from __future__ import annotations

from typing import Any


def _value_of(param: dict[str, Any]) -> Any:
    for key, value in param.items():
        if key.startswith("value"):
            return value
    if "resource" in param:
        return param["resource"]
    if "part" in param:
        return parameters_to_dict({"parameter": param["part"]})
    return None


def parameters_to_dict(parameters: dict[str, Any]) -> dict[str, Any]:
    """Platta ut en Parameters-resurs till en dict.

    Upprepade namn (t.ex. ``property`` och ``designation`` i ``$lookup``)
    blir listor. Parametrar med ``part`` blir nästlade dictar.
    """
    out: dict[str, Any] = {}
    for param in parameters.get("parameter", []):
        name = param.get("name")
        value = _value_of(param)
        if name in _REPEATING:
            out.setdefault(name, []).append(value)
        elif name in out:
            if not isinstance(out[name], list):
                out[name] = [out[name]]
            out[name].append(value)
        else:
            out[name] = value
    return out


# Parametrar som kan förekomma flera gånger och därför alltid blir listor.
_REPEATING = {"property", "designation", "match", "message", "issues"}
