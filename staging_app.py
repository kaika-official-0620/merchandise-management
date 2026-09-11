"""Render staging entrypoint. Production continues to use render_app:app."""
import os

from staging_environment import (StagingConfigurationError, check_upload_disk,
    install_outbound_guard, prepare_environment, register_staging_boundary,
    seed_test_data, staging_bootstrap, validate_environment)


config = validate_environment()
prepare_environment(config)
install_outbound_guard()
check_upload_disk("/opt/render/project/src/static/uploads")
with staging_bootstrap(config):
    import render_app
    if render_app.RUNTIME_SOURCE != "source":
        raise StagingConfigurationError("Staging requires the current Python source; bytecode fallback is refused.")
    check_upload_disk(render_app.module)
    seed_test_data(render_app.module, {role: os.environ["STAGING_" + role.upper() + "_PASSWORD"]
                                      for role in ("admin", "normal", "business")})
    register_staging_boundary(render_app.module)

app = render_app.app
print("[STAGING] Ready: isolated database/disk; fictional test accounts; external delivery and billing disabled.", flush=True)
