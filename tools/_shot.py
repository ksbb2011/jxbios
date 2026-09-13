import cv2, time
from pathlib import Path
from core.devices.imouse_client import ImouseClient
c = ImouseClient(); c.detect()
d = c.pick_online(c.list_devices())
i = c.screenshot(d.id)
p = Path("docs/_probe/shots") / ("home_" + time.strftime("%H%M%S") + ".png")
p.parent.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(p), i)
print(p, i.shape)
