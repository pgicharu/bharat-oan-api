import os
import re
import base64
import json
import httpx
from dotenv import load_dotenv

from helpers.utils import get_logger, curl_escape_single_quoted

load_dotenv()

logger = get_logger(__name__)


def remove_urls(text):
    return re.sub(r'https?://\S+', '', text)


def text_to_speech_bhashini(text, source_lang='en', gender='female', sampling_rate=8000):
    url = 'https://dhruva-api.bhashini.gov.in/services/inference/pipeline'
    service_id = "tts"
    headers = {
        'Accept': '*/*',
        'Authorization': os.getenv('MEITY_API_KEY_VALUE'),
        'Content-Type': 'application/json',
    }
    data = {
        "pipelineTasks": [
            {
                "taskType": "tts",
                "config": {
                    "language": {
                        "sourceLanguage": source_lang
                    },
                    "serviceId": "",
                    "gender": gender,
                    "samplingRate": sampling_rate
                }
            }
        ],
        "inputData": {
            "input": [
                {
                    "source": text
                }
            ]
        }
    }

    logger.info(
        "TTS Bhashini input | target_lang=%s gender=%s sampling_rate=%s text_length=%s",
        source_lang, gender, sampling_rate, len(text)
    )
    logger.info(
        "TTS Bhashini request payload | serviceId=%s payload=%s",
        service_id, json.dumps(data, ensure_ascii=False)
    )
    payload_str = json.dumps(data, ensure_ascii=False)
    payload_escaped = curl_escape_single_quoted(payload_str)
    curl = (
        "curl -X POST '%s' -H 'Authorization: <MEITY_API_KEY_VALUE>' -H 'Content-Type: application/json' -d '%s'"
    ) % (url, payload_escaped)
    logger.info(
        "TTS Bhashini external API | serviceId=%s curl=%s",
        service_id, curl
    )

    try:
        response = httpx.post(
            url,
            headers=headers,
            json=data,
            timeout=httpx.Timeout(30.0, read=60.0)
        )

        if response.status_code != 200:
            logger.error(
                "TTS Bhashini failed | status_code=%s serviceId=%s response=%s curl=%s",
                response.status_code, service_id, response.text[:500], curl
            )
            raise RuntimeError(
                "TTS Bhashini API error: %s %s" % (response.status_code, response.text[:500])
            )

        response_json = response.json()
        audio_content = response_json['pipelineResponse'][0]['audio'][0]['audioContent']
        audio_data = base64.b64decode(audio_content)
        logger.info(
            "TTS Bhashini output | target_lang=%s audio_size_bytes=%s",
            source_lang, len(audio_data)
        )
        return audio_data
    except httpx.HTTPStatusError as e:
        logger.error(
            "TTS Bhashini HTTP error | status_code=%s serviceId=%s message=%s curl=%s",
            e.response.status_code if e.response else None, service_id,
            (e.response.text if e.response else str(e))[:500], curl
        )
        raise
    except Exception as e:
        logger.error(
            "TTS Bhashini error | serviceId=%s error=%s message=%s curl=%s",
            service_id, type(e).__name__, str(e)[:1000], curl
        )
        raise