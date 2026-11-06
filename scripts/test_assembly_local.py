from pathlib import Path
from src.assemble import assemble

out = Path('output/test/assembly_test')
out.mkdir(parents=True, exist_ok=True)

cfg = {
    'video': {
        'width': 720,
        'height': 1280,
        'fps': 24,
        'cut_every_seconds': 2,
        'music_volume_db': -25,
        'color_grade': False,
        'add_progress_bar': False,
        'encode_preset': 'ultrafast',
        'crf': 25,
    }
}

clips = [
    Path(f'assets/local_visuals/{i:02}.png')
    for i in range(1, 4)
]
assert all(p.is_file() for p in clips)

result = assemble(
    cfg,
    clips,
    Path('output/test/voice_test.mp3'),
    Path('output/test/captions_test.srt'),
    out,
)

assert result.is_file()
print('ASSEMBLY TEST PASS:', result)
