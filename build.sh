#!/bin/bash
docker build -t quay.io/ntnu/autograde:latest .
docker push quay.io/ntnu/autograde:latest
