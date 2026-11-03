"""Run a minimal model workflow on an OP4300 simulator."""

import csv
import time
from pathlib import Path

from op_unified_controller_api_client import ProjectsApi
from op_unified_controller_api_client import (
    CommandValuesApi,
    PutCommandValueRequest,
    ValueInner,
)

from opalrt._internals._opalone._internal.apiclient import _ApiClient
from opalrt.dashboard import Panel
from opalrt.dashboard.commands import (
    NumericControl,
    NumericControlDataType,
    Slider,
    SliderDataType,
)
from opalrt.dashboard.displays import Graph
from opalrt.interfaces import IOInterfaceType
from opalrt.model import Constant, Gain, SimulationMode, Sum
from opalrt.project import Connection, create_project, get_projects
from opalrt.recorder import (
    DurationUnit,
    RecorderSignalAssignment,
    RecordingFileState,
    RecordingFileFormat,
    RecordingStatus,
    TriggerCondition,
)
from opalrt.session import LocalSessionConfig, start_session, terminate_session
from opalrt.simulator import Simulator, SimulatorState, get_simulators
from opalrt.simulator_setup import SimulatorSetup


INSTALLATION_PATH = r"C:\OPAL-RT\HYPERLAB\1.8.0.23"
OP4300_IP_ADDRESS = r"10.64.104.70"

PROJECT_NAME = "MinimalWorkflowOP4300"
MODEL_NAME = "OP4300Model"
SETUP_NAME = "OP4300 Real-Time"
TIME_STEP = 1e-5
STOP_TIME = 30.0
SETUP_ACTIVATION_TIMEOUT = 5.0
RECORDING_DURATION = 15.0
RECORDING_DURATION_TOLERANCE = 1e-9
RECORDING_FILE_TIMEOUT = 30.0
SIMULATION_STOP_TIMEOUT = 35.0
INITIAL_CONSTANT_1 = 1.0
INITIAL_CONSTANT_2 = 2.0
UPDATED_CONSTANT_1 = 2.0
UPDATED_CONSTANT_2 = 3.0
GAIN_VALUE = 2.0
EXPECTED_SUM = 5.0
EXPECTED_AO00 = GAIN_VALUE * EXPECTED_SUM
OUTPUT_TOLERANCE = 0.1
INPUT_UPDATE_DELAY = 2.0
AO00_ALIAS = "AO00"
SUM_ALIAS = "Sum"


def setup_simulator(project):
    matching_simulators = [
        item for item in get_simulators() if item.ip == OP4300_IP_ADDRESS
    ]
    if len(matching_simulators) > 1:
        raise RuntimeError(
            f"Multiple simulators are registered at {OP4300_IP_ADDRESS!r}."
        )
    simulator = (
        matching_simulators[0]
        if matching_simulators
        else Simulator(ip=OP4300_IP_ADDRESS)
    )
    if simulator.state is not SimulatorState.READY:
        raise RuntimeError(
            f"OP4300 simulator {simulator.name!r} is {simulator.state.value}; "
            "it must be ready before starting."
        )

    setup = SimulatorSetup(
        parent=project,
        name=SETUP_NAME,
        simulator=simulator,
        description="OP4300 real-time simulation setup",
    )
    setup.simulation_mode = SimulationMode.REAL_TIME
    setup.activate()

    activation_deadline = time.monotonic() + SETUP_ACTIVATION_TIMEOUT
    while not setup.is_active and time.monotonic() < activation_deadline:
        time.sleep(0.1)
    if not setup.is_active:
        raise TimeoutError(
            f"Simulator setup {setup.name!r} did not become active within "
            f"{SETUP_ACTIVATION_TIMEOUT:g} seconds."
        )
    print(f"Simulator setup {setup.name!r} is active.")
    return setup, simulator


def setup_simulation_parameters(setup):
    setup.time_step = TIME_STEP
    setup.stop_time = STOP_TIME
    print(
        f"Simulator setup complete: real-time mode, time_step={TIME_STEP:g}, "
        f"stop_time={STOP_TIME:g} seconds."
    )


def build_model(project, block_factories, connection_factory):
    model = project.model
    blocks = [factory(model) for factory in block_factories]

    for source, destination in connection_factory(blocks):
        model.add_link(from_port=source, to_port=destination)

    return model, blocks


def add_io_interface_connection(
    project,
    io_interfaces,
    interface_type,
    channel_name,
    signal,
):
    io_interface = io_interfaces[interface_type]
    io_channel = io_interface.channels[channel_name]
    if interface_type in (IOInterfaceType.ANALOG_IN, IOInterfaceType.DIGITAL_IN):
        source, destination = io_channel, signal
    elif interface_type in (
        IOInterfaceType.ANALOG_OUT,
        IOInterfaceType.DIGITAL_OUT,
    ):
        source, destination = signal, io_channel
    else:
        raise ValueError(f"Unsupported IO interface type: {interface_type!r}.")

    return Connection(parent=project, source=source, destination=destination)


def build_panel(project, panel, widget_factories, blocks, connection_factory):
    widgets = [factory(panel) for factory in widget_factories]
    for source, destination in connection_factory(widgets, blocks):
        Connection(parent=project, source=source, destination=destination)

    print(f"Panel {panel.name!r} created.")
    return widgets


def setup_recorder(project, signals, trigger_signal):
    recorder = project.recorder
    recorder.file_format = RecordingFileFormat.CSV
    recorder.duration_unit = DurationUnit.SECONDS
    recorder.total_duration = RECORDING_DURATION
    recorder.repeat_count = 1
    recorder.add_signals(
        [RecorderSignalAssignment(signal) for signal in signals]
    )
    recorder.trigger = trigger_signal
    trigger = recorder.trigger
    if trigger is None:
        raise RuntimeError("The recorder trigger was not configured.")
    trigger.condition = TriggerCondition.RISING
    trigger.level = EXPECTED_SUM
    recorder.enabled = True
    return recorder


def validate_recorder_file(
    recorder,
    expected_signal_values,
    expected_duration,
):
    recordings = recorder.recordings
    if not recordings:
        raise RuntimeError("The OP4300 recorder produced no recording.")
    recording = recordings[-1]
    file_deadline = time.monotonic() + RECORDING_FILE_TIMEOUT
    while True:
        recording_files = recording.files
        if recording_files and all(
            item.status
            in (RecordingFileState.COMPLETED, RecordingFileState.FAILED)
            for item in recording_files
        ):
            break
        if time.monotonic() >= file_deadline:
            raise TimeoutError(
                f"Recording files did not complete within "
                f"{RECORDING_FILE_TIMEOUT:g} seconds."
            )
        time.sleep(0.1)

    failed_files = [
        item.error
        for item in recording_files
        if item.status is RecordingFileState.FAILED
    ]
    if failed_files:
        raise RuntimeError(f"OP4300 recording failed: {failed_files!r}")

    completed_files = [
        item
        for item in recording_files
        if item.status is RecordingFileState.COMPLETED and item.path
    ]
    if not completed_files:
        raise RuntimeError("The OP4300 recorder produced no completed CSV file.")

    signal_aliases = [signal.alias for signal, _ in expected_signal_values]
    if len(signal_aliases) != len(set(signal_aliases)):
        raise ValueError("Expected recorder signals must have unique aliases.")

    selected_signals = {
        signal.datapoint_id: signal for signal in recorder.signals
    }
    for signal, _ in expected_signal_values:
        recorded_signal = selected_signals.get(signal.id)
        if recorded_signal is None:
            raise RuntimeError(
                f"The Recorder is missing signal {signal.alias!r}."
            )
        if recorded_signal.alias != signal.alias:
            raise RuntimeError(
                f"The Recorder reports alias {recorded_signal.alias!r} for "
                f"{recorded_signal.path!r}; expected {signal.alias!r}."
            )

    signal_values = {}
    recording_duration = None
    for recording_file in completed_files:
        with Path(recording_file.path).open(
            newline="", encoding="utf-8"
        ) as csv_file:
            reader = csv.reader(csv_file)
            columns = next(reader, None)
            type_row = next(reader, None)
            if columns is None or type_row is None:
                raise RuntimeError(
                    f"Recording file {recording_file.path!r} is missing CSV headers."
                )
            required_columns = (*signal_aliases, "timestamps")
            missing_columns = [
                column for column in required_columns if column not in columns
            ]
            if missing_columns:
                raise RuntimeError(
                    f"Recording file {recording_file.path!r} is missing "
                    f"column(s): {missing_columns!r}."
                )
            timestamp_column = columns.index("timestamps")
            rows = list(reader)

        samples_by_alias = {
            alias: [
                row[columns.index(alias)]
                for row in rows
                if row[columns.index(alias)]
            ]
            for alias in signal_aliases
        }
        timestamps = [
            float(row[timestamp_column]) for row in rows if row[timestamp_column]
        ]
        if any(not samples for samples in samples_by_alias.values()) or not timestamps:
            raise RuntimeError(
                f"Recording file {recording_file.path!r} is missing expected "
                "signal or timestamp samples."
            )
        signal_values = {
            alias: float(samples[-1])
            for alias, samples in samples_by_alias.items()
        }
        recording_duration = timestamps[-1] - timestamps[0] + TIME_STEP
        break

    for signal, expected_value in expected_signal_values:
        actual_value = signal_values[signal.alias]
        assert abs(actual_value - expected_value) <= OUTPUT_TOLERANCE, (
            f"Recorded {signal.alias} value {actual_value!r}; expected "
            f"{expected_value:g} +/- {OUTPUT_TOLERANCE:g}."
        )
    if recording_duration is None:
        raise RuntimeError("No recording duration was read from the completed CSV.")
    assert abs(recording_duration - expected_duration) <= RECORDING_DURATION_TOLERANCE, (
        f"Recorded duration was {recording_duration:g} seconds; expected "
        f"{expected_duration:g} +/- {RECORDING_DURATION_TOLERANCE:g} seconds."
    )
    if recording.status is RecordingStatus.FAILED:
        raise RuntimeError("The OP4300 recording status is FAILED.")
    print(
        f"Validated recorded signals {signal_values!r}; "
        f"duration={recording_duration:g} seconds."
    )


def run_workflow():
    project = None
    model = None
    start_session(LocalSessionConfig(INSTALLATION_PATH))
    try:
        existing_projects = [
            item for item in get_projects() if item.name == PROJECT_NAME
        ]
        for existing_project in existing_projects:
            existing_project.delete()
            print(f"Deleted existing project {PROJECT_NAME!r}.")

        project = create_project(PROJECT_NAME)
        print(f"Project {PROJECT_NAME!r} created.")

        setup, simulator = setup_simulator(project)
        setup_simulation_parameters(setup)

        def create_constant_1(parent):
            return Constant(
                parent=parent,
                name="Constant1",
                constant=INITIAL_CONSTANT_1,
            )

        def create_constant_2(parent):
            return Constant(
                parent=parent,
                name="Constant2",
                constant=INITIAL_CONSTANT_2,
            )

        def create_sum(parent):
            return Sum(parent=parent, name="Sum", operators="++")

        def create_gain(parent):
            return Gain(parent=parent, name="Gain", gain=GAIN_VALUE)

        def create_model_connections(model_blocks):
            first_constant, second_constant, total, amplifier = model_blocks
            return [
                (first_constant.out_port, total.in_port_1),
                (second_constant.out_port, total.in_port_2),
                (total.out_port, amplifier.in_port),
            ]

        model, blocks = build_model(
            project,
            block_factories=[
                create_constant_1,
                create_constant_2,
                create_sum,
                create_gain,
            ],
            connection_factory=create_model_connections,
        )
        model.name = MODEL_NAME
        print(f"Model {model.name!r} created.")
        constant_1, constant_2, sum_block, gain = blocks
        sum_block.out_signal.alias = SUM_ALIAS
        if sum_block.out_signal.alias != SUM_ALIAS:
            raise RuntimeError(
                f"Sum output has alias {sum_block.out_signal.alias!r}; "
                f"expected {SUM_ALIAS!r}."
            )

        io_interfaces = project.io_interfaces
        ao00 = io_interfaces[IOInterfaceType.ANALOG_OUT].channels[
            "P4/Channel 00"
        ]
        if ao00.alias != AO00_ALIAS:
            raise RuntimeError(
                f"Analog Output/P4/Channel 00 has alias {ao00.alias!r}; "
                f"expected {AO00_ALIAS!r}."
            )
        add_io_interface_connection(
            project=project,
            io_interfaces=io_interfaces,
            interface_type=IOInterfaceType.ANALOG_OUT,
            channel_name="P4/Channel 00",
            signal=gain.out_signal,
        )

        panel = Panel(parent=project, name="OP4300 Results", order=1)

        def create_graph(parent):
            graph = Graph(parent=parent, name="AO00 and Sum")
            graph.autoscale_y = True
            return graph

        def create_slider(parent):
            slider = Slider(parent=parent, name="Constant1 Slider")
            slider.data_type = SliderDataType.REAL
            slider.minimum = 0.0
            slider.maximum = 5.0
            return slider

        def create_numeric_control(parent):
            control = NumericControl(parent=parent, name="Constant2 Control")
            control.data_type = NumericControlDataType.REAL
            return control

        def create_panel_connections(panel_widgets, model_blocks):
            graph, slider, control = panel_widgets
            first_constant, second_constant, total, _ = model_blocks
            return [
                (ao00, graph.data_y),
                (total.out_signal, graph.data_y),
                (slider.data, first_constant.parameters["Constant"]),
                (control.data, second_constant.parameters["Constant"]),
            ]

        panel_widgets = build_panel(
            project,
            panel,
            widget_factories=[
                create_graph,
                create_slider,
                create_numeric_control,
            ],
            blocks=blocks,
            connection_factory=create_panel_connections,
        )
        _, constant_1_slider, constant_2_control = panel_widgets
        recorder = setup_recorder(
            project,
            signals=[ao00, sum_block.out_signal],
            trigger_signal=sum_block.out_signal,
        )

        print(
            f"Running {model.name!r} on {simulator.name!r} for {STOP_TIME:g} "
            "seconds in real-time mode."
        )
        model.run()
        time.sleep(INPUT_UPDATE_DELAY)
        if not model.is_running:
            raise RuntimeError("The OP4300 model stopped before input updates.")

        CommandValuesApi(_ApiClient.get()).put_command_value(
            command_id=constant_1_slider.data.id,
            put_command_value_request=PutCommandValueRequest(
                value=[ValueInner(UPDATED_CONSTANT_1)]
            ),
        )
        print(f"Updated Constant1 slider to {UPDATED_CONSTANT_1:g}.")
        time.sleep(INPUT_UPDATE_DELAY)

        CommandValuesApi(_ApiClient.get()).put_command_value(
            command_id=constant_2_control.data.id,
            put_command_value_request=PutCommandValueRequest(
                value=[ValueInner(UPDATED_CONSTANT_2)]
            ),
        )
        expected_sum = UPDATED_CONSTANT_1 + UPDATED_CONSTANT_2
        assert expected_sum == EXPECTED_SUM, (
            f"Model inputs sum to {expected_sum:g}; expected {EXPECTED_SUM:g}."
        )
        print(
            f"Updated Constant2 numeric control; expected Sum is {expected_sum:g} "
            f"and expected AO00 is {GAIN_VALUE:g} x {expected_sum:g}."
        )

        stop_deadline = time.monotonic() + SIMULATION_STOP_TIMEOUT
        while model.is_running and time.monotonic() < stop_deadline:
            time.sleep(0.1)
        if model.is_running:
            model.stop()
        if model.is_running:
            raise AssertionError("The OP4300 simulation is still running.")

        validate_recorder_file(
            recorder,
            expected_signal_values=[
                (ao00, EXPECTED_AO00),
                (sum_block.out_signal, EXPECTED_SUM),
            ],
            expected_duration=RECORDING_DURATION,
        )
    finally:
        try:
            if model is not None and model.is_running:
                model.stop()
        finally:
            try:
                if project is not None:
                    ProjectsApi(_ApiClient.get()).close_project(project.id)
                    print(f"Closed project {PROJECT_NAME!r}.")
            finally:
                terminate_session()


if __name__ == "__main__":
    run_workflow()
