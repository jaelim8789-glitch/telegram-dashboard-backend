"""Test script to call Ollama API and print the full response to inspect the 'usage' object."""

import asyncio
import httpx
# Target the LOCAL Ollama instance directly
OLLAMA_API_BASE_NATIVE = "http://127.0.0.1:11434/api"  # Native API base on local machine
OLLAMA_API_KEY = ""  # Assuming no key is needed for local native API
OLLAMA_MODEL = "0xIbra/supergemma4-26b-uncensored-gguf-v2:Q4_K_M"  # Use the same model for consistency



async def test_ollama_native_response():
    print("--- Testing LOCAL Native Ollama API (http://127.0.0.1:11434/api/chat) ---")
    if not OLLAMA_API_BASE_NATIVE:
        print("Error: OLLAMA_API_BASE_NATIVE is not configured.")
        return

    url = f"{OLLAMA_API_BASE_NATIVE}/chat"  # Native API endpoint
    headers = {
        "Content-Type": "application/json",
        # Authorization header might be different or absent for native API, adjust if needed
        # "Authorization": f"Bearer {OLLAMA_API_KEY}" if OLLAMA_API_KEY else ""
    }
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"}
        ],
        "stream": False,  # Keep it False to get the full response object
        "keep_alive": "24h"  # Apply keep_alive for native API call
        # Removed "options" wrapper, placing keep_alive at root level for native API
    }

    print(f"Calling LOCAL Native Ollama API at: {url}")
    print(f"Payload: {payload}")
    print("-" * 20)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

            print("Full LOCAL Ollama API Response (Native):")
            print(data)
            print("-" * 20)

            # Check for native performance metrics in the main response object
            # Fields like load_duration, prompt_eval_duration, eval_duration, eval_count are typically here
            load_duration = data.get("load_duration", "Not Found")
            prompt_eval_duration = data.get("prompt_eval_duration", "Not Found")
            eval_duration = data.get("eval_duration", "Not Found")
            prompt_eval_count = data.get("prompt_eval_count", "Not Found")
            eval_count = data.get("eval_count", "Not Found")
            total_duration = data.get("total_duration", "Not Found")

            print("\nNative Performance Metrics found in LOCAL response:")
            print(f"  load_duration: {load_duration}")
            print(f"  prompt_eval_duration: {prompt_eval_duration}")
            print(f"  eval_duration: {eval_duration}")
            print(f"  prompt_eval_count: {prompt_eval_count}")
            print(f"  eval_count: {eval_count}")
            print(f"  total_duration: {total_duration}")

    except httpx.HTTPStatusError as e:
        print(f"HTTP Error {e.response.status_code}: {e.response.text}")
    except httpx.RequestError as e:
        print(f"Request Error: {e}")
    except Exception as e:
        print(f"Unexpected Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_ollama_native_response())