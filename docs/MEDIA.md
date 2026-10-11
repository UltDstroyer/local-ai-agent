# Desktop video and owner-voice narration

Status: opt-in desktop companion only. The autonomous worker has no
video, microphone, or voice profile access.

## Desktop recording (OBS Studio)

Install OBS Studio in your ordinary desktop session. Create a capture scene,
set your recording output location, then enable the OBS WebSocket server bound
to localhost with a password. OBS WebSocket protocol v5 is required.

In your desktop terminal:

    read -rs CTX_AGENT_OBS_PASSWORD
    export CTX_AGENT_OBS_PASSWORD
    ctx-agent-media record start
    # interact with the desktop; OBS visibly indicates recording
    ctx-agent-media record stop
    unset CTX_AGENT_OBS_PASSWORD

The companion sends OBS StartRecord/StopRecord requests over loopback.
No silent recording fallback is attempted. The autonomous AI cannot invoke
this operation. The OBS WebSocket password must not be saved in Git or notes.

## Narration matching your own voice

1. Record 5-12 seconds of your speech as a WAV with an accurate transcript.
2. From your desktop user account, run:

       ctx-agent-media enroll /path/to/sample.wav --transcript "Words I actually said."

3. Independently install F5-TTS and a supported Torch backend for your GPU:
   https://github.com/SWivid/F5-TTS

4. Make its f5-tts_infer-cli command available on PATH, then run:

       ctx-agent-media narrate "This is my project." ~/Videos/project-voice.wav
       ctx-agent-media mux ~/Videos/capture.mp4 ~/Videos/project-voice.wav ~/Videos/final.mp4

The reference voice and transcript are kept in your personal local data folder
under ctx-agent/media, with private file permissions. The system service does
not receive those files and cannot synthesize speech autonomously. F5-TTS can
match aspects of your voice but is not guaranteed to reproduce it exactly.

F5-TTS and ROCm are optional and not packaged by this flake. GPU usage for
F5-TTS is not governed by the Ollama profiles; for long clips use idle hours
or explicitly invoke manual maximum mode.

Not yet implemented: an AI-approved per-video desktop broker, automatic scene
selection, subtitle creation, mic capture, fully autonomous editing, or GUI
per-request recording approvals. Screen capture always requires an active,
owner-configured OBS scene.

You may only use voice samples for which you have permission.
