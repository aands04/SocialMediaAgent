from pathlib import Path

from PIL import Image, ImageDraw


class Renderer:
    sizes={"feed":(1080,1350),"story":(1080,1920)}
    def __init__(self,root:Path): self.root=root
    def render(self,kind:str,target:str,data:dict)->Path:
        if kind not in self.sizes: raise ValueError("Unbekanntes Format")
        out=self.root/target; out.parent.mkdir(parents=True,exist_ok=True)
        image=Image.new("RGB",self.sizes[kind],data.get("primary_color","#172554")); draw=ImageDraw.Draw(image)
        lines=[data.get("post_type","SPIELTAG").upper(),data["home_team"],"vs.",data["away_team"],data["kickoff"],data.get("venue") or ""]
        y=160
        for line in lines: draw.text((90,y),str(line),fill=data.get("secondary_color","white"),stroke_width=1); y+=100
        image.save(out,"PNG"); self.validate(out,kind); return out
    def validate(self,path:Path,kind:str):
        with Image.open(path) as image:
            if image.size!=self.sizes[kind] or image.format!="PNG": raise ValueError("Grafikprüfung fehlgeschlagen")
