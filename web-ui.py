#!/usr/bin/env python3

from transformers import WhisperProcessor, WhisperForConditionalGeneration, pipeline
from pyannote.audio import Pipeline
from pyannote.database.loader import RTTMLoader
from pytube import YouTube
from pydub import AudioSegment
from dotenv import load_dotenv
import gradio as gr
import moviepy.editor as mp
import datetime
import logging
import os
import shutil

# Load .env file
load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Tokens, etc
# Hugging Face token: https://huggingface.co/docs/hub/security-tokens#user-access-tokens
HUGGINGFACE_AUTH_TOKEN = os.getenv('HUGGINGFACE_AUTH_TOKEN')
logging.info(f"Hugging Face token: {HUGGINGFACE_AUTH_TOKEN}")

TEMP_VIDEO_FILE = "temp/input.mp4"
TEMP_AUDIO_FILE = "temp/input.wav"
TEMP_DIARIZATION_FILE = "temp/diarization.rttm"


def ensure_dir(path):
    """Make sure director from the given path exists"""
    dir = os.path.dirname(path)
    if dir:
        os.makedirs(dir, exist_ok=True)


def fetch_youtube(url, output_video_file, output_audio_file):
    """Fetch WAV audio from given youtube URL"""

    logging.info(f"Fetching audio from Youtube URL: {url}")

    ensure_dir(output_video_file)
    ensure_dir(output_audio_file)

    video_stream = YouTube(url).streams.first()
    video_stream.download(filename=output_video_file)

    video = mp.VideoFileClip(output_video_file)
    video.audio.write_audiofile(output_audio_file, codec='pcm_s16le')

    logging.info("Done fetching audio form YouTube")


def extract_wav_from_video(video_file, output_audio_file):
    """Extract WAV audio from given video file"""

    logging.info(f"Extracting audio from video file: {video_file}")

    ensure_dir(output_audio_file)
    video = mp.VideoFileClip(video_file)
    video.audio.write_audiofile(output_audio_file, codec='pcm_s16le')

    logging.info("Done extracting audio from video file")

def transform_mp3_to_wav(mp3_file, output_audio_file):
    """Transform MP3 audio to WAV audio"""

    logging.info(f"Transforming MP3 audio to WAV audio: {mp3_file}")

    ensure_dir(output_audio_file)
    
    audio = mp.AudioFileClip(mp3_file)
    audio.write_audiofile(output_audio_file, codec='pcm_s16le')

    logging.info("Done transforming MP3 audio to WAV audio")

TIMESTAMP_FORMAT = "%H:%M:%S.%f"
base_time = datetime.datetime(1970, 1, 1)


def format_timestamp(seconds):
    """Format timestamp in SubViewer format: https://wiki.videolan.org/SubViewer/"""

    date = base_time + datetime.timedelta(seconds=seconds)
    return date.strftime(TIMESTAMP_FORMAT)[:-4]


def extract_audio_track(input_file, start_time, end_time, track_file):
    """Extract and save part of given audio file"""

    # Load the WAV file
    audio = AudioSegment.from_wav(input_file)

    # Calculate the start and end positions in milliseconds
    start_ms = start_time * 1000
    end_ms = end_time * 1000

    # Extract the desired segment
    track = audio[start_ms:end_ms]

    track.export(track_file, format="mp3")


def generate_speaker_diarization(audio_file):
    """Generate speaker diarization for given audio file"""

    logging.info(f"Generating speaker diarization... audio file: {audio_file}")

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.0",
        use_auth_token=HUGGINGFACE_AUTH_TOKEN)

    result = pipeline(audio_file)

    logging.info("Done generating spearer diarization")

    with open(TEMP_DIARIZATION_FILE, "w") as rttm:
        result.write_rttm(rttm)

    logging.info(f"Wrote diarization file: {TEMP_DIARIZATION_FILE}", )

    return result


def generate_transcription(diarization, model, collar):
    """Generate transcription from given diarization object"""

    logging.info(f"Generating transcription... model: {model}")

    pipe = pipeline(
        "automatic-speech-recognition",
        model=f"openai/whisper-{model}",
        chunk_length_s=30,
        device="mps"
    )

    # Create directory for tracks
    shutil.rmtree("output-tracks", ignore_errors=True)
    os.mkdir("output-tracks")

    result = []
    for turn, _, speaker in diarization.support(collar).itertracks(yield_label=True):
        part_file = f"output-tracks/{round(turn.start, 2)}-{speaker}.mp3"
        part_path = os.path.join(os.curdir, part_file)
        extract_audio_track(TEMP_AUDIO_FILE, turn.start, turn.end, part_file)

        part_data = None
        with open(part_path, "rb") as audio_content:
            part_data = audio_content.read()

        output = pipe(part_data, batch_size=8, return_timestamps=False)
        text = output['text']

        result.append({
            'start': turn.start,
            'end': turn.end,
            'speaker': speaker,
            'text': text.strip(),
            'track_path': part_path
        })

    logging.info(f"Done generating transcripion tracks: {len(result)}")
    return result


def format_transcription(transcription):
    """Format transcription in SubViewer format: https://wiki.videolan.org/SubViewer/"""

    result = ""
    for t in transcription:
        result += f"{format_timestamp(t['start'])},{format_timestamp(t['end'])}\n{t['speaker']}: {t['text']}\n\n"
    return result


def save_transcription(transcription, file_name = ''):
    """Save trainscription in SubViewer format to file."""

    logging.info(f"Saving transcripion... to file: {file_name}_output.sub")
    f = open(file_name + "_output.sub", "w")
    for t in transcription:
        # Format in SubViewer format: https://wiki.videolan.org/SubViewer/
        f.write(
            f"{format_timestamp(t['start'])},{format_timestamp(t['end'])}\n{t['speaker']}: {t['text']}\n\n")
    f.close()

    logging.info("Done saving transcripion")


def process_video(youtube_url, video_file, audio_file, model, collar, skip, progress=gr.Progress()):
    """Main function to run the whole procesessing pipeline."""
    file_name = ''
    if "Extract audio" not in skip:
        if video_file:
            progress(0.1, desc="Processing video file...")
            extract_wav_from_video(
                video_file,
                output_audio_file=TEMP_AUDIO_FILE,
            )
            file_name = video_file.split('/')[-1].split('.')[0]
        elif audio_file:
            print("Processing audio file...")
            progress(0.1, desc="Processing audio file...")
            file_name = audio_file.split('/')[-1].split('.')[0]
            transform_mp3_to_wav(
                audio_file,
                output_audio_file=TEMP_AUDIO_FILE,
            )
        elif youtube_url:
            progress(0.1, desc="Downloading video...")
            fetch_youtube(
                youtube_url,
                output_audio_file=TEMP_AUDIO_FILE,
                output_video_file=TEMP_VIDEO_FILE
            )
        else:
            raise gr.Error("Provide either Youtube URL or video file")
    else:
        progress(0.1, desc=f"Reusing local file... {TEMP_AUDIO_FILE}")
        logging.debug(f"Reusing local file {TEMP_AUDIO_FILE}")

    if "Speaker diarization" not in skip:
        progress(
            0.5, desc="Generating speaker diarization... (this may take a while)")
        diarization = generate_speaker_diarization(TEMP_AUDIO_FILE)
    else:
        progress(0.5, desc="Reusing local dirization file...")
        logging.info(
            f"Reusing local dirization file... {TEMP_DIARIZATION_FILE}")
        rttm = RTTMLoader(TEMP_DIARIZATION_FILE).loaded_
        diarization = rttm['input']

    progress(0.8, desc="Generating transcription... (this may take a while)")
    transcription = generate_transcription(diarization, model, collar)

    progress(1.0, desc="Done!")
    output = format_transcription(transcription)
    save_transcription(transcription, file_name)
    return output


with gr.Blocks() as ui:
    gr.Markdown(
        """
        # Video Transcription Tool
        Upload a video file or paste a YouTube URL. Then press "Start" to generate a transcription.
        """
    )

    with gr.Row():
        with gr.Column(scale=4):
            video_file = gr.Video()
            audio_file = gr.Audio(label="Input Audio", type="filepath", format="mp3")
            
            youtube_url = gr.Textbox(
                label="YouTube URL",
                placeholder="https://www.youtube.com/watch?v=...",
                value="https://www.youtube.com/watch?v=4V2C0X4qqLY",
            )

            # Reset youtube URL on video upload
            video_file.upload(lambda: None, [], youtube_url)
            youtube_url.input(lambda: None, [], video_file)
            
            # Reset youtube URL on audio upload
            audio_file.upload(lambda: None, [], youtube_url)
            youtube_url.input(lambda: None, [], audio_file)

            start_btn = gr.Button("Start")

        with gr.Column(scale=1):
            pass

        with gr.Column(scale=3, variant="panel"):
            gr.Markdown(
                """
                ### Parameters
                """
            )
            collar = gr.Number(
                label="Collar (seconds)",
                info="Will join two consecutive tracks if they are closer than this value",
                value=0.5,
                minimum=0,
                maximum=30,
                step=0.1)

            model = gr.Dropdown([
                "tiny", "tiny.en",
                "base", "base.en",
                "small", "small.en",
                "medium", "medium.en",
                "large", "large-v2"],
                value="base",
                label="Whisper Model",
                info="Large models take more time to process and require more memory."
            )

            skip_group = gr.CheckboxGroup(
                ["Extract audio", "Speaker diarization"],
                label="Skip Steps",
                info="Skip already made steps, to speed up processing."
            )

    gr.Markdown(
        """
        ## Output
        Transcription will appear here. It will also be saved to `output.sub` file.
        """
    )

    output_text = gr.Textbox(
        label="Output text",
        max_lines=25,
        show_copy_button=True,
    )

    start_btn.click(
        fn=process_video,
        inputs=[youtube_url, video_file, audio_file, model, collar, skip_group],
        outputs=[output_text]
    )

ui.queue()
ui.launch(inbrowser=True)