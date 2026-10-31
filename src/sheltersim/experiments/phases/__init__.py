"""The tick phases of OfflineSimulationController, one module per phase.

Each module holds one mixin of the controller. Its `_run_*_phase` method is the phase's
list of steps, each a named method, so one step can be replaced on its own.
"""
