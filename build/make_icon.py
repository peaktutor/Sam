"""Draw the app icon (a rounded navy tile with "P4") to p4_app/assets/p4.ico. Needs Pillow; run once by hand."""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'p4_app', 'assets', 'p4.ico')
S = 512
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=96, fill='#1f2a44')
font = ImageFont.truetype('segoeuib.ttf', 250)
box = d.textbbox((0, 0), 'P4', font=font)
d.text(((S - box[2] - box[0]) / 2, (S - box[3] - box[1]) / 2 - 18), 'P4', font=font, fill='white')
d.rounded_rectangle((116, 396, S - 116, 420), radius=12, fill='#2f6fde')    # the accent colour used in the window
os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print('written', OUT)
