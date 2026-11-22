# Agent Guidelines

This is Sandbx, a python3 wrapper around podman to create sandboxed environments for AI agents

## Project goals
- Isolate AI agents from the host using disposable libkrun MicroVMs launched from podman
- Provide fallback support for rootless podman containers
- Sandbx can only access two host directories, the agents state and the project directory.
- Support most commonly used coding agents
- Enable switching between multiple persistent login sessions
- Enable Host -> Guest access 
- Enable Guest -> host access on limited set of ports
