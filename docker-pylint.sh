#!/bin/bash
set -x
docker build -t $USER/coronado-workerplugin .
docker run --rm --entrypoint=pylint $USER/coronado-workerplugin WorkerPlugin
