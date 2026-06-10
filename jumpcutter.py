import subprocess
from audiotsm import phasevocoder
from audiotsm.io.wav import WavReader, WavWriter
from scipy.io import wavfile
import numpy as np
import re
import math
from shutil import copyfile, rmtree
import os
import argparse
from pytube import YouTube
import logging


def download_youtube_video(url: str):
    available_video_list = YouTube(url).streams.first()
    if available_video_list is None:
        raise RuntimeError(
            "given URL does not return any YouTube videos (only youtube videos supported currently)"
        )
    name = available_video_list.download()
    newname = name.replace(" ", "_")
    os.rename(name, newname)
    return newname


def get_max_volume(audio_data: np.ndarray) -> float:
    maxv = float(np.max(audio_data))
    minv = float(np.min(audio_data))
    return max(maxv, -minv)


def copy_frame(input_frame_index: int, output_frame_index: int) -> bool:
    src = TEMP_FOLDER + "/frame{:06d}".format(input_frame_index + 1) + ".jpg"
    dst = TEMP_FOLDER + "/newFrame{:06d}".format(output_frame_index + 1) + ".jpg"
    if not os.path.isfile(src):
        return False
    copyfile(src, dst)
    if output_frame_index % 20 == 19:
        print(str(output_frame_index + 1) + " time-altered frames saved.")
    return True


def get_output_filename_from_input_filename(input_filename: str) -> str:
    dot_index = input_filename.rfind(".")  # TODO: What if input file includes a dot?
    return f"{input_filename[:dot_index]} ALTERED {input_filename[dot_index:]}"


def create_path(path: str | os.PathLike, safe_guard: bool = True) -> None:
    assert (not safe_guard) or (
        not os.path.exists(path)
    ), f"The filepath {path} already exists. Don't want to overwrite it. Aborting."

    try:
        os.mkdir(path)
    except OSError:
        assert (
            False
        ), f"Creation of the directory {path} failed. (The TEMP folder may already exist. Delete or rename it, and try again.)"


def delete_path(path: str | os.PathLike) -> None:
    try:
        rmtree(path, ignore_errors=False)
    except OSError:
        logging.error(f"Deletion of the directory {path} failed")
        logging.error(OSError)


logging.info("Initializing...")
parser = argparse.ArgumentParser(
    description="Modifies a video file to play at different speeds when there is sound vs. silence."
)
parser.add_argument("--input_file", type=str, help="the video file you want modified")
parser.add_argument("--url", type=str, help="A youtube url to download and process")
parser.add_argument(
    "--output_file",
    type=str,
    default="",
    help="the output file. (optional. if not included, it'll just modify the input file name)",
)
parser.add_argument(
    "--silent_threshold",
    type=float,
    default=0.03,
    help='the volume amount that frames\' audio needs to surpass to be consider "sounded". It ranges from 0 (silence) to 1 (max volume)',
)
parser.add_argument(
    "--sounded_speed",
    type=float,
    default=1.00,
    help="the speed that sounded (spoken) frames should be played at. Typically 1.",
)
parser.add_argument(
    "--silent_speed",
    type=float,
    default=5.00,
    help="the speed that silent frames should be played at. 999999 for jumpcutting.",
)
parser.add_argument(
    "--frame_margin",
    type=float,
    default=1,
    help="some silent frames adjacent to sounded frames are included to provide context. How many frames on either the side of speech should be included? That's this variable.",
)
parser.add_argument(
    "--sample_rate",
    type=float,
    default=44100,
    help="sample rate of the input and output videos",
)
parser.add_argument(
    "--frame_rate",
    type=float,
    default=30,
    help="frame rate of the input and output videos. optional... I try to find it out myself, but it doesn't always work.",
)
parser.add_argument(
    "--frame_quality",
    type=int,
    default=3,
    help="quality of frames to be extracted from input video. 1 is highest, 31 is lowest, 3 is the default.",
)

args = parser.parse_args()

frame_rate = args.frame_rate
SAMPLE_RATE = args.sample_rate
SILENT_THRESHOLD = args.silent_threshold
FRAME_SPREADAGE = args.frame_margin
NEW_SPEED = [args.silent_speed, args.sounded_speed]
if args.url != None:
    INPUT_FILE = download_youtube_video(args.url)
else:
    INPUT_FILE = args.input_file
URL = args.url
FRAME_QUALITY = args.frame_quality

assert INPUT_FILE != None, "why u put no input file, that dum"

if len(args.output_file) >= 1:
    OUTPUT_FILE = args.output_file
else:
    OUTPUT_FILE = get_output_filename_from_input_filename(INPUT_FILE)

TEMP_FOLDER = "TEMP"
AUDIO_FADE_ENVELOPE_SIZE = 400  # smooth out transitiion's audio by quickly fading in/out (arbitrary magic number whatever)
logging.info("Initialization done!")

create_path(TEMP_FOLDER)

logging.info("Extracting video data")
# extract all frames
command = f"ffmpeg -i '{INPUT_FILE}' -qscale:v {str(FRAME_QUALITY)} '{TEMP_FOLDER}/frame%06d.jpg' -hide_banner"
subprocess.call(command, shell=True)

# extract audio
command = f"ffmpeg -i '{INPUT_FILE}' -ab 160k -ac 2 -ar {str(SAMPLE_RATE)} -vn '{TEMP_FOLDER}/audio.wav'"
subprocess.call(command, shell=True)

# write video parameters to file
command = f"ffmpeg -i '{TEMP_FOLDER}/input.mp4' 2>&1"
f = open(TEMP_FOLDER + "/params.txt", "w")
subprocess.call(
    command, shell=True, stdout=f
)  # TODO: just put them directly in a variable


audio_sample_rate, audio_raw_data = wavfile.read(TEMP_FOLDER + "/audio.wav")
audio_samples_count = audio_raw_data.shape[0]
max_volume = get_max_volume(audio_raw_data)

f = open(TEMP_FOLDER + "/params.txt", "r+")
pre_params = f.read()
f.close()
params = pre_params.split("\n")
for line in params:
    m = re.search(r"Stream #.*Video.* (\d*) fps", line)
    if m is not None:
        frame_rate = float(m.group(1))  # TODO: What if the line is missing in the file?

audio_samples_count_per_video_frame = audio_sample_rate / frame_rate

audio_frames_count = math.ceil(
    audio_samples_count / audio_samples_count_per_video_frame
)  # TODO: may lead to disjunction if video is long. Also why not just use the frames themselves? Like whats the difference between audio frames and video frames?

has_loud_audio = np.zeros((audio_frames_count))

# Chunking audio by frames and nothing if has audio (or not)
for i in range(audio_frames_count):
    start = int(i * audio_samples_count_per_video_frame)
    end = min(int((i + 1) * audio_samples_count_per_video_frame), audio_samples_count)
    chunk = audio_raw_data[start:end]
    chunk_max_volume = float(get_max_volume(chunk)) / max_volume
    if chunk_max_volume >= SILENT_THRESHOLD:
        has_loud_audio[i] = 1

# appending chunks that surround loud frames
chunks = [[0, 0, 0]]  # Format: [last chunk, current chunk, shouldIncludeFrame?]
frames_included = np.zeros((audio_frames_count))
for i in range(audio_frames_count):
    start = int(max(0, i - FRAME_SPREADAGE))
    end = int(min(audio_frames_count, i + 1 + FRAME_SPREADAGE))
    frames_included[i] = np.max(has_loud_audio[start:end])
    if i >= 1 and frames_included[i] != frames_included[i - 1]:  # Did we flip?
        chunks.append([chunks[-1][1], i, frames_included[i - 1]])
chunks.append([chunks[-1][1], audio_frames_count, frames_included[i - 1]])
chunks = chunks[1:]


output_audio_data = np.zeros((0, audio_raw_data.shape[1]))
output_pointer = 0

last_existing_frame = None
for chunk in chunks:
    audioChunk = audio_raw_data[
        int(chunk[0] * audio_samples_count_per_video_frame) : int(
            chunk[1] * audio_samples_count_per_video_frame
        )
    ]

    # editing audio speed in chunk
    sFile = TEMP_FOLDER + "/tempStart.wav"
    eFile = TEMP_FOLDER + "/tempEnd.wav"
    wavfile.write(sFile, SAMPLE_RATE, audioChunk)
    with WavReader(sFile) as reader:
        with WavWriter(eFile, reader.channels, reader.samplerate) as writer:
            tsm = phasevocoder(reader.channels, speed=NEW_SPEED[int(chunk[2])])
            tsm.run(reader, writer)
    _, alteredAudioData = wavfile.read(eFile)
    leng = alteredAudioData.shape[0]
    endPointer = output_pointer + leng
    output_audio_data = np.concatenate((output_audio_data, alteredAudioData / max_volume))

    # smoothing out transitiion's audio by quickly fading in/out
    if leng < AUDIO_FADE_ENVELOPE_SIZE:
        output_audio_data[output_pointer:endPointer] = (
            0  # audio is less than 0.01 sec, let's just remove it.
        )
    else:
        premask = np.arange(AUDIO_FADE_ENVELOPE_SIZE) / AUDIO_FADE_ENVELOPE_SIZE
        mask = np.repeat(
            premask[:, np.newaxis], 2, axis=1
        )  # make the fade-envelope mask stereo
        output_audio_data[
            output_pointer : output_pointer + AUDIO_FADE_ENVELOPE_SIZE
        ] *= mask
        output_audio_data[endPointer - AUDIO_FADE_ENVELOPE_SIZE : endPointer] *= 1 - mask

    # keep frames with relevant audio
    startOutputFrame = math.ceil(output_pointer / audio_samples_count_per_video_frame)
    endOutputFrame = math.ceil(endPointer / audio_samples_count_per_video_frame)
    for outputFrame in range(startOutputFrame, endOutputFrame):
        inputFrame = int(
            chunk[0] + NEW_SPEED[int(chunk[2])] * (outputFrame - startOutputFrame)
        )
        didItWork = copy_frame(inputFrame, outputFrame)
        if didItWork:
            last_existing_frame = inputFrame
        else:
            copy_frame(last_existing_frame, outputFrame)

    output_pointer = endPointer

wavfile.write(TEMP_FOLDER + "/audioNew.wav", SAMPLE_RATE, output_audio_data)


command = f"ffmpeg -framerate {str(frame_rate)} -i '{TEMP_FOLDER}/newFrame%06d.jpg' -i '{TEMP_FOLDER}/audioNew.wav' -strict -2 '{OUTPUT_FILE}'"
subprocess.call(command, shell=True)

delete_path(TEMP_FOLDER)
