"""Put up to four cameras side by side in one picture, with their sound in one track.

The media server runs this while someone watches a mix: mixer.py <path of the mix> <path of a camera>...
"""
import base64
import json
import signal
import socket
import subprocess
import sys

MEDIA = "/media-config/mediamtx.yml"
USER = "nestunifi-mixer"  # The media server user that reads the cameras and publishes the mix.
SOUND = "-audio"  # What follows a camera's path to name the path of its sound.
SOURCE = "rtsp://%s:%s@127.0.0.1:8554/"
WIDTH, HEIGHT, RATE = 640, 360, 15  # One tile. A row holds two.


def describe(path, username, password, timeout=6):
    """Ask the media server for a path as this user: 200 with a publisher, 404 without, 401 when refused."""
    basic = base64.b64encode((username + ":" + password).encode()).decode()
    query = (f"DESCRIBE rtsp://mediamtx:8554/{path} RTSP/1.0\r\nCSeq: 1\r\n"
             f"Accept: application/sdp\r\nAuthorization: Basic {basic}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", 8554), timeout=timeout) as conn:
        conn.sendall(query.encode())
        with conn.makefile("rb") as response:
            return int(response.readline(256).split()[1])


def size(tiles):
    return 2 * WIDTH, (tiles + 1) // 2 * HEIGHT


def command(target, videos, sounds, source):
    """The ffmpeg for one mix. `videos` has an entry per tile: a path, or None for a camera without a picture
    now, whose tile stays black."""
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    for path in [path for path in videos if path] + sounds:
        # Every source counts time from its first packet. Opening them without a look ahead keeps those moments
        # together, so no camera runs behind the others.
        args += ["-rtsp_transport", "tcp", "-analyzeduration", "0", "-i", source + path]
    graph, used = [], 0
    for tile, path in enumerate(videos):
        if path:
            graph.append("[%d:v]fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
                         "setsar=1[v%d]" % (used, RATE, WIDTH, HEIGHT, WIDTH, HEIGHT, tile))
            used += 1
        else:
            graph.append("color=black:size=%dx%d:rate=%d[v%d]" % (WIDTH, HEIGHT, RATE, tile))
    graph.append("".join("[v%d]" % tile for tile in range(len(videos))) + "xstack=inputs=%d:layout=%s:fill=black:shortest=1[v]" % (
        len(videos), "|".join("%d_%d" % (tile % 2 * WIDTH, tile // 2 * HEIGHT) for tile in range(len(videos)))))
    args += ["-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
             "-g", str(2 * RATE), "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "3000k"]
    if sounds:
        graph.append("".join("[%d:a]" % (used + i) for i in range(len(sounds)))
                     + "amix=inputs=%d:duration=shortest:normalize=0[a]" % len(sounds))
        args += ["-map", "[a]", "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    # A source that ends, ends the mix. Whoever watches asks for it again, and that starts it with what there is then.
    return args + ["-filter_complex", ";".join(graph), "-shortest", "-f", "rtsp", "-rtsp_transport", "tcp", source + target]


def main(target, cameras):
    with open(MEDIA) as f:
        password = next(user["pass"] for user in json.load(f)["authInternalUsers"] if user.get("user") == USER)

    def live(path):
        try:
            return describe(path, USER, password, timeout=3) == 200
        except (OSError, ValueError, IndexError):
            return False
    videos = [path if live(path) else None for path in cameras]
    if not any(videos):
        return 1
    sounds = [path + SOUND for path in videos if path and live(path + SOUND)]
    absent = [path for path, video in zip(cameras, videos) if not video]
    silent = [path + SOUND for path in videos if path and path + SOUND not in sounds]
    mix = subprocess.Popen(command(target, videos, sounds, SOURCE % (USER, password)))
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: mix.terminate())
    checks = 0
    while True:
        try:
            return mix.wait(timeout=30)
        except subprocess.TimeoutExpired:
            checks += 1
        # A camera that is back gets its tile by starting over. Its sound can come a moment after its picture,
        # so that is looked for during the first two minutes only: many cameras never have any.
        if any(live(path) for path in absent + (silent if checks <= 4 else [])):
            mix.terminate()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
