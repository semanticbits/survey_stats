#!/bin/bash

curl -O "https://s3.amazonaws.com/owh-stat-data/versions/1.1.1/cache.tgz" && \
    rm -rf cache && \
    tar -xvzf cache.tgz && \
    pip install -e .
