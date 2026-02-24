# Changelog

All notable changes to this project will be documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/)

## [Unreleased]

### Added
- Repository skeleton with FastAPI stub endpoints (`POST /zoom/webhook`, `GET /results/{meeting_uuid}`, `GET /health`)
- `src/config.py` — pydantic-settings `Settings` class with all environment variable definitions
- `src/results.py` — `ResultStore` ABC, `InMemoryResultStore`, and `LivenessResult`/`SessionStatus` dataclasses
- Multi-stage `Dockerfile` with FFmpeg, MediaPipe system dependencies (`libgl1-mesa-glx`, `libglib2.0-0`)
- `docker-compose.yml` with healthcheck
- GitHub Actions CI pipeline (lint, typecheck, test, docker build + smoke tests)
- GitHub Actions release pipeline (GHCR image push on version tags)
- Unit tests for `InMemoryResultStore` and `Settings`
