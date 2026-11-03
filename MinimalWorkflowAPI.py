"""Temporary HYPERLAB API workflow for debugging Part I before Part II.

Workflow
--------
Part I: Import and run the BoostConverter example on localhost.
1. Use the hardcoded HYPERLAB installation folder below.
2. Import examples/boost-converter/Boost Converter.zip as BoostConverter.
3. Start a local HYPERLAB API session.
4. Find a project named BoostConverter and delete that exact project if present.
   Project.delete() removes the project and all its underlying resources.
5. Import the example zip as a project named BoostConverter.
6. Select the localhost simulator setup, set accelerated mode, and activate it.
7. Configure simulation parameters separately: timestep 1e-5 and no stop time.
8. Run the model, set model widget "slider1" duty cycle to 45, then stop it.

Part II: To be implemented after Part I is debugged.
- Create a custom model with two constants, a sum, and a gain of 1.
- Add a dashboard with controls for the constants, a display of the sum, and a
  graph of AO00. Verify these widget types and methods against the installed
  dashboard API before implementing them.
- Configure the OP4300 setup for real-time mode, a 1e-5 timestep, and 30 seconds.
- Record AO00 for 15 seconds with a rising trigger at sum=5.
- Vary the model inputs, verify the expected values, wait for simulation to
  finish, and validate the resulting recording.

Safety: Part I intentionally replaces an existing project named BoostConverter.
"""

import time
from pathlib import Path

from op_unified_controller_api_client import (
    CommandValuesApi,
    ProjectsApi,
    PutCommandValueRequest,
    ValueInner,
)

from opalrt._internals._opalone._internal.apiclient import _ApiClient
from opalrt.dashboard.commands import Slider
from opalrt.model import SimulationMode
from opalrt.exceptions import NotFoundException
from opalrt.project import find_project, import_project
from opalrt.session import LocalSessionConfig, start_session, terminate_session
from opalrt.simulator import get_localhost_simulator
from opalrt.simulator_setup import SimulatorSetup


INSTALLATION_PATH = r"C:\OPAL-RT\HYPERLAB\1.8.0.23"
PROJECT_NAME = "BoostConverter"
EXAMPLE_FOLDER = "boost-converter"
EXAMPLE_ARCHIVE = "Boost Converter.zip"
SLIDER_WIDGET_NAME = "slider1"
SLIDER_VALUE = 45
RUN_SECONDS = 15.0
SETUP_ACTIVATION_TIMEOUT = 5.0
TIMESTEP = 1e-5

if not PROJECT_NAME:
    raise RuntimeError("PROJECT_NAME must not be empty.")

installation_path = Path(INSTALLATION_PATH)
project_zip = (
    installation_path
    / "examples"
    / EXAMPLE_FOLDER
    / EXAMPLE_ARCHIVE
)
if not project_zip.is_file():
    raise FileNotFoundError(
        f"Example project archive was not found: {project_zip}. "
        "Expected the configured example archive under the installation folder."
    )

start_session(LocalSessionConfig(str(installation_path)))
model = None

try:
    try:
        existing_project = find_project(PROJECT_NAME)
    except NotFoundException:
        existing_project = None

    if existing_project is None:
        print(f"No existing project named {PROJECT_NAME!r}; proceeding to import.")
    else:
        print(
            f"Deleting existing project {PROJECT_NAME!r} before importing "
            f"{project_zip}."
        )
        existing_project.delete()

    project = import_project(str(project_zip), import_name=PROJECT_NAME)
    if project.name != PROJECT_NAME:
        raise AssertionError(
            f"Imported project name is {project.name!r}; expected {PROJECT_NAME!r}."
        )

    model = project.model
    localhost = get_localhost_simulator()
    setup = next(
        (
            candidate
            for candidate in project.simulator_setups.values()
            if candidate.simulator is not None
            and candidate.simulator.id == localhost.id
        ),
        None,
    )
    if setup is None:
        setup = SimulatorSetup(
            parent=project,
            name="Localhost Accelerated",
            simulator=localhost,
            description="Localhost accelerated setup for Part I testing",
        )

    # Simulator setup: select localhost, choose accelerated mode, and activate.
    setup.simulation_mode = SimulationMode.ACCELERATED
    setup.activate()
    activation_deadline = time.monotonic() + SETUP_ACTIVATION_TIMEOUT
    while not setup.is_active and time.monotonic() < activation_deadline:
        time.sleep(0.1)
    if not setup.is_active:
        raise RuntimeError(
            f"Localhost simulator setup {setup.name!r} did not become active "
            f"within {SETUP_ACTIVATION_TIMEOUT:g} seconds."
        )
    print(f"Simulator setup {setup.name!r} is active.")

    # Simulation parameters: configure timing for the selected mode.
    setup.time_step = TIMESTEP
    setup.stop_time = float("inf")

    print(
        f"Running {PROJECT_NAME!r} on localhost in accelerated mode "
        f"(time_step={TIMESTEP:g}, no stop time) for {RUN_SECONDS:g} seconds."
    )
    model.run()

    slider = model.widgets.get(SLIDER_WIDGET_NAME)
    if slider is None:
        raise LookupError(
            f"No model widget named {SLIDER_WIDGET_NAME!r} was found."
        )
    if not isinstance(slider, Slider):
        raise TypeError(
            f"Widget {SLIDER_WIDGET_NAME!r} is {type(slider).__name__}, "
            "not a Slider."
        )

    # Interact with the model's duty-cycle slider by issuing a command value.
    time.sleep(3.0)  # Wait for the model to start running before sending commands.
    CommandValuesApi(_ApiClient.get()).put_command_value(
        command_id=slider.data.id,
        put_command_value_request=PutCommandValueRequest(
            value=[ValueInner(SLIDER_VALUE)]
        ),
    )
    print(f"Set duty-cycle slider {SLIDER_WIDGET_NAME!r} to {SLIDER_VALUE}.")

    time.sleep(RUN_SECONDS)
    model.stop()
    if model.is_running:
        raise AssertionError("The simulation is still running after stop().")

    ProjectsApi(_ApiClient.get()).close_project(project.id)
    print(f"Closed project {PROJECT_NAME!r}.")
    print("The simulation on localhost stopped successfully.")

finally:
    try:
        if model is not None and model.is_running:
            model.stop()
    finally:
        terminate_session()
