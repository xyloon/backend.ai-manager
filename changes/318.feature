Make the `mgr dbshell` command to be smarter to auto-detect the halfstack db container and use the container-provided psql command for maximum compatibility, with an optinal ability to set the container ID or name explicitly via `--psql-container` option and forward additional arguments to the psql command