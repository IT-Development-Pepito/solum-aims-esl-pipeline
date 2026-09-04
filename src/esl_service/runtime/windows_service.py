"""Windows Service wrapper over ``ServiceHost`` (FR-029, #28).

Service Control Manager requests map one-to-one onto the tested lifecycle:
start builds the host and starts it, pause and continue quiesce and resume
scheduling, stop pauses scheduling and stops the tick loop within its
deadline. The API listener runs on a worker thread for the life of the
service, bound to ``ESL_INTERNAL_HOST``:``ESL_INTERNAL_PORT`` only.

This module is the only place pywin32's service framework is imported, and
it is imported lazily so the rest of the runtime, and every test, works
without it. Registering the service is an administrator action:

    python -m esl_service.runtime.windows_service --startup auto install
    python -m esl_service.runtime.windows_service start

``scripts/install-service.ps1`` wraps those steps with the service account
and the environment variables the service needs.
"""

import os
import sys
import threading
from typing import Any

from esl_service.runtime.host import Host, build_host

DEFAULT_SERVICE_NAME = "SOLUM_ESL_PIPELINE"


def _framework() -> tuple[Any, Any, Any]:
    import servicemanager  # type: ignore[import-untyped]
    import win32event  # type: ignore[import-untyped]
    import win32serviceutil  # type: ignore[import-untyped]

    return servicemanager, win32event, win32serviceutil


def _service_class() -> type[Any]:
    servicemanager, win32event, win32serviceutil = _framework()
    framework_base: Any = win32serviceutil.ServiceFramework

    class EslPipelineService(framework_base):  # type: ignore[misc]
        _svc_name_ = os.environ.get("ESL_WINDOWS_SERVICE_NAME", DEFAULT_SERVICE_NAME)
        _svc_display_name_ = "SOLUM ESL Pipeline"
        _svc_description_ = (
            "Scheduler and internal operations API for the ESL replacement pipeline."
        )

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._host: Host | None = None
            self._server: Any = None

        # -- SCM entry points ----------------------------------------------

        def SvcDoRun(self) -> None:
            servicemanager.LogInfoMsg(f"{self._svc_name_}: starting")
            self._host = build_host()
            self._host.service.start()
            self._serve_in_background(self._host)
            win32event.WaitForSingleObject(self._stop_event, -1)

        def SvcStop(self) -> None:
            self.ReportServiceStatus(_service_stop_pending())
            if self._host is not None:
                self._host.service.stop(reason="SCM stop")
                self._shutdown_server()
            win32event.SetEvent(self._stop_event)

        def SvcPause(self) -> None:
            if self._host is not None:
                self._host.service.pause(reason="SCM pause")
            self.ReportServiceStatus(_service_paused())

        def SvcContinue(self) -> None:
            if self._host is not None:
                self._host.service.resume(reason="SCM continue")
            self.ReportServiceStatus(_service_running())

        # -- the API listener ------------------------------------------------

        def _serve_in_background(self, host: Host) -> None:
            import uvicorn

            from esl_service.web.app import create_app

            app = create_app(
                operations=host.operations,
                authenticator=host.authenticator,
                health=host.health,
                scheduler=host.scheduler,
                audit=host.ports,
                run_evidence=host.run_evidence,
                configuration_version_id=host.context.configuration_version_id,
                mode=host.context.mode,
            )
            config = uvicorn.Config(
                app, host=host.settings.internal_host, port=host.settings.internal_port, log_level="info"
            )
            self._server = uvicorn.Server(config)
            threading.Thread(target=self._server.run, name="esl-api", daemon=True).start()

        def _shutdown_server(self) -> None:
            if self._server is not None:
                self._server.should_exit = True

    return EslPipelineService


def _service_stop_pending() -> int:
    import win32service  # type: ignore[import-untyped]

    return int(win32service.SERVICE_STOP_PENDING)


def _service_paused() -> int:
    import win32service

    return int(win32service.SERVICE_PAUSED)


def _service_running() -> int:
    import win32service

    return int(win32service.SERVICE_RUNNING)


def main(argv: list[str] | None = None) -> None:
    """Install, start, stop, or run the service through pywin32's helper."""

    _, _, win32serviceutil = _framework()
    win32serviceutil.HandleCommandLine(_service_class(), argv=argv or sys.argv)


if __name__ == "__main__":
    main()
