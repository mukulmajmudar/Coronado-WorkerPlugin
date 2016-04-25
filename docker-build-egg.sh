#!/bin/bash
set -x
docker build -t $USER/coronado-workerplugin .
mkdir -p dist
docker run --rm \
    -e USERID=$EUID \
    -v `pwd`/dist:/root/WorkerPlugin/dist \
    $USER/coronado-workerplugin
