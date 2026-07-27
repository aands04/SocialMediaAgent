from pathlib import Path

from PIL import Image, ImageDraw


class RenderValidationError(ValueError): pass
class Renderer:
    sizes={"feed":(1080,1350),"story":(1080,1920)}
    def __init__(self,root:Path): self.root=root
    def render(self,kind:str,target:str,data:dict)->Path:
        if kind not in self.sizes: raise ValueError("Unbekanntes Format")
        required=("home_team","away_team","kickoff","post_type")
        missing=[key for key in required if not str(data.get(key,"")).strip()]
        if missing: raise RenderValidationError(f"Pflichtangaben fehlen: {', '.join(missing)}")
        out=self.root/target; out.parent.mkdir(parents=True,exist_ok=True)
        image=Image.new("RGB",self.sizes[kind],data.get("primary_color") or "#172554"); draw=ImageDraw.Draw(image)
        lines=[data["post_type"].upper(),data["home_team"],"vs.",data["away_team"],data["kickoff"],data.get("venue") or "Ort folgt"]
        y=160; max_width=self.sizes[kind][0]-180
        for line in lines:
            text=str(line)
            if draw.textbbox((0,0),text)[2]>max_width: raise RenderValidationError(f"Pflichtangabe passt nicht in Textbereich: {text}")
            draw.text((90,y),text,fill=data.get("secondary_color") or "white",stroke_width=1); y+=100
        image.save(out,"PNG"); self.validate(out,kind); return out
    def validate(self,path:Path,kind:str)->dict:
        if not path.is_file() or path.stat().st_size<=0: raise RenderValidationError("Grafikdatei fehlt oder ist leer")
        with Image.open(path) as image:
            image.load()
            if image.size!=self.sizes[kind] or image.format!="PNG": raise RenderValidationError("Auflösung oder PNG-Format ungültig")
            if image.mode=="RGBA" and image.getchannel("A").getextrema()==(0,0): raise RenderValidationError("Grafik ist vollständig transparent")
            colors=image.convert("RGB").getcolors(maxcolors=image.width*image.height)
            if colors is not None and len(colors)<2: raise RenderValidationError("Grafik ist einfarbig und vermutlich leer")
            return {"kind":kind,"width":image.width,"height":image.height,"format":image.format,"bytes":path.stat().st_size,"colors":len(colors) if colors else ">max"}
