"""On-demand probe that checks configured Gemini models for manual function calling."""

from agent.gemini import GeminiClient, GeminiConfig, GoogleGenAIProvider


FUNCTIONS = [{
    "name": "probe",
    "description": "Return the supplied probe token unchanged.",
    "parameters": {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    },
}]


def main() -> int:
    try:
        from agent.gemini import load_gemini_config
        config = load_gemini_config()
        provider = GoogleGenAIProvider(config.api_key, config.timeout_seconds)
    except Exception as exc:
        print(f"Gemini probe could not start: {exc}")
        return 1

    passed = 0
    for model in config.models:
        single = GeminiConfig(config.api_key, (model,), config.timeout_seconds)
        client = GeminiClient(single, provider)
        try:
            reply = client.generate(
                [{"role": "user", "parts": [{
                    "text": "Use the probe function with token set to MODEL_PROBE."
                }]}],
                FUNCTIONS,
                run_id="model-probe",
                step=1,
            )
            if reply.function_call and reply.function_call.name == "probe" and \
                    reply.function_call.arguments.get("token") == "MODEL_PROBE":
                print(f"PASS {model}: manual function calling works")
                passed += 1
            else:
                print(f"FAIL {model}: did not return the expected probe function call")
        except Exception as exc:
            cause = exc.__cause__ or exc
            detail = str(cause).replace(config.api_key, "[REDACTED]")
            code = getattr(cause, "code", None)
            print(f"FAIL {model}: {type(cause).__name__} (code={code}): {detail}")
    print(f"{passed}/{len(config.models)} configured model(s) passed.")
    return 0 if passed == len(config.models) else 1


if __name__ == "__main__":
    raise SystemExit(main())
