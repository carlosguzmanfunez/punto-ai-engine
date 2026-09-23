"""Contratos y configuración del scheduler; la ejecución se incorpora en fases posteriores."""

from punto.scheduler.settings import SchedulerLimits, load_scheduler_limits

__all__ = ["SchedulerLimits", "load_scheduler_limits"]
