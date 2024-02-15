from cog import BasePredictor, Input, Path, BaseModel
from pydub import AudioSegment
from typing import Any
from whisperx.audio import N_SAMPLES, log_mel_spectrogram

import gc
import math
import os
import shutil
import whisperx
import tempfile
import time
import torch

compute_type = "float16"  # change to "int8" if low on GPU mem (may reduce accuracy)
device = "cuda"
whisper_arch = "./models/faster-whisper-large-v3"


class ModelOutput(BaseModel):
    segments: Any
    detected_language: str


class Predictor(BasePredictor):
    def setup(self):
        source_folder = './models/vad'
        destination_folder = '../root/.cache/torch'
        file_name = 'whisperx-vad-segmentation.bin'

        os.makedirs(destination_folder, exist_ok=True)

        source_file_path = os.path.join(source_folder, file_name)
        if os.path.exists(source_file_path):
            destination_file_path = os.path.join(destination_folder, file_name)

            if not os.path.exists(destination_file_path):
                shutil.copy(source_file_path, destination_folder)

    def predict(
            self,
            audio_file: Path = Input(description="Audio file"),
            language: str = Input(
                description="ISO code of the language spoken in the audio, specify None to perform language detection",
                default=None),
            language_detection_min_prob: float = Input(
                description="If language is not specified, then the language will be detected recursively on different "
                            "parts of the file until it reaches the given probability",
                default=0
            ),
            language_detection_max_tries: int = Input(
                description="If language is not specified, then the language will be detected following the logic of "
                            "language_detection_min_prob parameter, but will stop after the given max retries",
                default=5
            ),
            initial_prompt: str = Input(
                description="Optional text to provide as a prompt for the first window",
                default=None),
            batch_size: int = Input(
                description="Parallelization of input audio transcription",
                default=64),
            temperature: float = Input(
                description="Temperature to use for sampling",
                default=0),
            vad_onset: float = Input(
                description="VAD onset",
                default=0.500),
            vad_offset: float = Input(
                description="VAD offset",
                default=0.363),
            align_output: bool = Input(
                description="Aligns whisper output to get accurate word-level timestamps",
                default=False),
            diarization: bool = Input(
                description="Assign speaker ID labels",
                default=False),
            huggingface_access_token: str = Input(
                description="To enable diarization, please enter your HuggingFace token (read). You need to accept "
                            "the user agreement for the models specified in the README.",
                default=None),
            min_speakers: int = Input(
                description="Minimum number of speakers if diarization is activated (leave blank if unknown)",
                default=None),
            max_speakers: int = Input(
                description="Maximum number of speakers if diarization is activated (leave blank if unknown)",
                default=None),
            debug: bool = Input(
                description="Print out compute/inference times and memory usage information",
                default=False)
    ) -> ModelOutput:
        with torch.inference_mode():
            asr_options = {
                "temperatures": [temperature],
                "initial_prompt": initial_prompt
            }

            vad_options = {
                "vad_onset": vad_onset,
                "vad_offset": vad_offset
            }

            if language is None:
                audio_duration = get_audio_duration(audio_file)
                segments_duration_ms = 30000

                language_detection_max_tries = min(
                    language_detection_max_tries,
                    math.floor(audio_duration / segments_duration_ms)
                )

                segments_starts = distribute_segments_equally(audio_duration, segments_duration_ms,
                                                              language_detection_max_tries)

                print("Detecting languages on segments starting at " + ', '.join(map(str, segments_starts)))

                detected_language_details = detect_language(audio_file, segments_starts, language_detection_min_prob,
                                                            language_detection_max_tries, asr_options, vad_options)

                detected_language_code = detected_language_details["language"]
                detected_language_prob = detected_language_details["probability"]
                detected_language_iterations = detected_language_details["iterations"]

                print(f"Detected language {detected_language_code} ({detected_language_prob:.2f}) after "
                      f"{detected_language_iterations} iterations.")

                language = detected_language_details["language"]

            start_time = time.time_ns() / 1e6

            model = whisperx.load_model(whisper_arch, device, compute_type=compute_type, language=language,
                                        asr_options=asr_options, vad_options=vad_options)

            if debug:
                elapsed_time = time.time_ns() / 1e6 - start_time
                print(f"Duration to load model: {elapsed_time:.2f} ms")

            start_time = time.time_ns() / 1e6

            audio = whisperx.load_audio(audio_file)

            if debug:
                elapsed_time = time.time_ns() / 1e6 - start_time
                print(f"Duration to load audio: {elapsed_time:.2f} ms")

            start_time = time.time_ns() / 1e6

            result = model.transcribe(audio, batch_size=batch_size)
            detected_language = result["language"]

            if debug:
                elapsed_time = time.time_ns() / 1e6 - start_time
                print(f"Duration to transcribe: {elapsed_time:.2f} ms")

            gc.collect()
            torch.cuda.empty_cache()
            del model

            if detected_language in whisperx.alignment.DEFAULT_ALIGN_MODELS_TORCH or detected_language in whisperx.alignment.DEFAULT_ALIGN_MODELS_HF:
                if align_output:
                    result = align(audio, result, debug)
                elif diarization:
                    result = diarize(audio, result, debug, huggingface_access_token, min_speakers, max_speakers)

                if debug:
                    print(f"max gpu memory allocated over runtime: {torch.cuda.max_memory_reserved() / (1024 ** 3):.2f} GB")

        return ModelOutput(
            segments=result["segments"],
            detected_language=detected_language
        )


def get_audio_duration(file_path):
    return len(AudioSegment.from_file(file_path))


def detect_language(full_audio_file_path, segments_starts, language_detection_min_prob,
                    language_detection_max_tries, asr_options, vad_options, iteration=1):
    model = whisperx.load_model(whisper_arch, device, compute_type=compute_type, asr_options=asr_options,
                                vad_options=vad_options)

    start_ms = segments_starts[iteration - 1]

    audio_segment_file_path = extract_audio_segment(full_audio_file_path, start_ms, 30000)

    audio = whisperx.load_audio(audio_segment_file_path)

    audio_segment_file_path.unlink()

    model_n_mels = model.model.feat_kwargs.get("feature_size")
    segment = log_mel_spectrogram(audio[: N_SAMPLES],
                                  n_mels=model_n_mels if model_n_mels is not None else 80,
                                  padding=0 if audio.shape[0] >= N_SAMPLES else N_SAMPLES - audio.shape[0])
    encoder_output = model.model.encode(segment)
    results = model.model.model.detect_language(encoder_output)
    language_token, language_probability = results[0][0]
    language = language_token[2:-2]

    print(f"Iteration {iteration} - Detected language: {language} ({language_probability:.2f})")

    gc.collect()
    torch.cuda.empty_cache()
    del model

    if language_probability >= language_detection_min_prob or iteration >= language_detection_max_tries:
        return {
            "language": language,
            "probability": language_probability,
            "iterations": iteration
        }

    return detect_language(full_audio_file_path, segments_starts, language_detection_min_prob,
                           language_detection_max_tries, asr_options, vad_options, iteration + 1)


def extract_audio_segment(input_file_path, start_time_ms, duration_ms):
    input_file_path = Path(input_file_path) if not isinstance(input_file_path, Path) else input_file_path

    audio = AudioSegment.from_file(input_file_path)

    end_time_ms = start_time_ms + duration_ms
    extracted_segment = audio[start_time_ms:end_time_ms]

    file_extension = input_file_path.suffix

    with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
        temp_file_path = Path(temp_file.name)
        extracted_segment.export(temp_file_path, format=file_extension.lstrip('.'))

    return temp_file_path


def distribute_segments_equally(total_duration, segments_duration, iterations):
    """
    This function calculates the start times for segments to be equally distributed across an audio file.

    Parameters:
    - total_duration (int): The duration of the audio file in milliseconds.
    - segments_duration (int): The duration of each segment in milliseconds.
    - iterations (int): The number of segments to extract.

    Returns:
    - list[int]: A list of start times for each segment in milliseconds.
    """

    # Calculate the total available duration for segments to be placed
    # This considers removing the duration of one segment from the total duration
    # to ensure segments don't go beyond the total duration
    available_duration = total_duration - segments_duration

    # Calculate the spacing between the starts of each segment
    # If iterations is 1, avoid division by zero by setting spacing to 0
    if iterations > 1:
        spacing = available_duration // (iterations - 1)
    else:
        spacing = 0

    # Generate the list of start times for each segment
    start_times = [i * spacing for i in range(iterations)]

    # Ensure the last segment starts so that it ends exactly at the end of the audio if iterations > 1
    if iterations > 1:
        start_times[-1] = total_duration - segments_duration

    return start_times


def align(audio, result, debug):
    start_time = time.time_ns() / 1e6

    model_a, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
    result = whisperx.align(result["segments"], model_a, metadata, audio, device,
                            return_char_alignments=False)

    if debug:
        elapsed_time = time.time_ns() / 1e6 - start_time
        print(f"Duration to align output: {elapsed_time:.2f} ms")

    gc.collect()
    torch.cuda.empty_cache()
    del model_a

    return result


def diarize(audio, result, debug, huggingface_access_token, min_speakers, max_speakers):
    start_time = time.time_ns() / 1e6

    diarize_model = whisperx.DiarizationPipeline(model_name='pyannote/speaker-diarization@2.1',
                                                 use_auth_token=huggingface_access_token, device=device)
    diarize_segments = diarize_model(audio, min_speakers=min_speakers, max_speakers=max_speakers)

    result = whisperx.assign_word_speakers(diarize_segments, result)

    if debug:
        elapsed_time = time.time_ns() / 1e6 - start_time
        print(f"Duration to diarize segments: {elapsed_time:.2f} ms")

    gc.collect()
    torch.cuda.empty_cache()
    del diarize_model

    return result
