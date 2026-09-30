"""Deterministic, native-looking weather card renderer (no AI imagery)."""
from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from .models import WeatherData
from .provider import WMO_CODES

W, H = 1600, 1000
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def _font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)

def _theme(w: WeatherData):
    try: now = datetime.fromisoformat(w.local_time or "").hour
    except ValueError:
        try: now = datetime.now(ZoneInfo(w.location.timezone)).hour
        except Exception: now = 12
    night = now < 6 or now >= 20
    code = w.weather_code
    if code >= 95: base = ((25,31,51),(58,67,91)); accent=(151,177,219)
    elif code in (65,67,82): base=((42,54,76),(80,99,125)); accent=(151,204,235)
    elif code in (61,62,63,80,81): base=((55,76,99),(113,137,158)); accent=(155,211,236)
    elif code in (45,48): base=((106,122,136),(176,187,191)); accent=(235,242,243)
    elif code >= 71: base=((91,116,142),(175,195,212)); accent=(238,247,255)
    elif night: base=((14,28,59),(35,63,105)); accent=(176,205,255)
    elif now < 8: base=((112,133,167),(231,185,142)); accent=(255,230,190)
    elif now >= 17: base=((102,85,111),(225,157,105)); accent=(255,220,165)
    else: base=((61,139,188),(153,207,220)); accent=(255,244,206)
    return base, accent, night

def _icon(draw, x, y, code, scale=1.0, night=False):
    """Minimal vector weather symbols: a single consistent stroke language."""
    c = (245,249,255) if night else (255,255,255); muted=(191,219,238)
    r=int(29*scale); width=max(3,int(5*scale))
    if code in (0,1):
        draw.ellipse((x-r,y-r,x+r,y+r), fill=(255,210,104), outline=(255,235,165), width=width)
        for i in range(8):
            import math
            a=i*math.pi/4; draw.line((x+int(math.cos(a)*(r+10)),y+int(math.sin(a)*(r+10)),x+int(math.cos(a)*(r+22)),y+int(math.sin(a)*(r+22))), fill=(255,225,135), width=width)
    elif code in (45,48):
        for yy in (y-12,y+2,y+16): draw.line((x-42,yy,x+42,yy), fill=c, width=width)
    else:
        draw.ellipse((x-50,y-5,x+7,y+28), fill=c); draw.ellipse((x-21,y-27,x+29,y+28), fill=c); draw.ellipse((x+7,y-9,x+53,y+28), fill=c)
        if code >= 95: draw.line((x+5,y+34,x-5,y+60,x+12,y+48,x+4,y+70), fill=(255,220,112), width=width)
        elif code >= 51 and code < 70 or code in (80,81,82):
            for dx in (-25,0,25): draw.line((x+dx,y+39,x+dx-7,y+60), fill=(151,211,242), width=width)
        elif code >= 71: draw.line((x-22,y+42,x-4,y+60,x+13,y+42,x+30,y+60), fill=(225,241,255), width=width)

def _txt(draw, xy, text, size, fill=(255,255,255), bold=False, anchor=None):
    draw.text(xy, str(text), font=_font(size,bold), fill=fill, anchor=anchor)

def _metric(draw, x, y, label, value, accent):
    _txt(draw,(x,y),label.upper(),16,(190,215,232)); _txt(draw,(x,y+26),value,25,(255,255,255),True)

def render_weather_card(weather: WeatherData) -> bytes:
    """Render a 1600x1000 PNG from only values present in WeatherData."""
    (top, bottom), accent, night = _theme(weather)
    im=Image.new("RGB",(W,H)); px=im.load()
    for y in range(H):
        t=y/(H-1); col=tuple(int(top[i]*(1-t)+bottom[i]*t) for i in range(3))
        for x in range(W): px[x,y]=col
    d=ImageDraw.Draw(im,"RGBA")
    # restrained atmospheric texture, not a stock photo
    for y in range(0,H,4): d.line((0,y,W,y),fill=(255,255,255,5),width=1)
    d.rounded_rectangle((48,42,1552,958), radius=34, fill=(8,20,38,42), outline=(255,255,255,55), width=2)
    loc=weather.location
    _txt(d,(92,84),loc.name,39,(255,255,255),True)
    _txt(d,(92,133),", ".join(p for p in (loc.admin1,loc.country) if p and p != loc.name),19,(207,225,238))
    local=weather.local_time or ""
    clock=local.split("T")[-1][:5] if "T" in local else local[-5:]
    date=weather.local_date or (local.split("T")[0] if local else "")
    _txt(d,(1508,86),clock,30,(255,255,255),True,anchor="ra")
    _txt(d,(1508,128),date,18,(207,225,238),anchor="ra")
    d.line((92,180,1508,180),fill=(255,255,255,70),width=2)
    _icon(d,1260,310,weather.weather_code,1.75,night)
    _txt(d,(92,235),f"{round(weather.temp_c)}°",130,(255,255,255),True)
    _txt(d,(98,375),weather.condition,34,(255,255,255),True)
    _txt(d,(98,425),f"Feels like {round(weather.feels_like_c)}°",22,(211,229,240))
    _txt(d,(98,470),f"Today  {round(weather.temp_max_c)}° / {round(weather.temp_min_c)}°",22,(211,229,240))
    # metrics, only show returned values
    aqi = weather.air_quality.get("aqi") if weather.air_quality else None
    metrics=[("Humidity",f"{weather.humidity}%"), ("Wind",f"{round(weather.wind_speed_kmh)} km/h {weather.wind_direction}"), ("Visibility",f"{weather.visibility_km:g} km" if weather.visibility_km is not None else None), ("Pressure",f"{round(weather.pressure_hpa)} hPa" if weather.pressure_hpa is not None else None), ("UV index",f"{weather.uv_index:g}" if weather.uv_index is not None else None), ("Dew point",f"{round(weather.dew_point_c)}°" if weather.dew_point_c is not None else None), ("Cloud cover",f"{weather.cloud_cover}%" if weather.cloud_cover is not None else None), ("Rain chance",f"{weather.precipitation_probability}%" if weather.precipitation_probability is not None else None), ("Wind gusts",f"{round(weather.wind_gusts_kmh)} km/h" if weather.wind_gusts_kmh is not None else None), ("AQI",str(aqi) if aqi is not None else None)]
    metrics=[m for m in metrics if m[1] is not None]
    d.rounded_rectangle((92,540,1508,730),radius=20,fill=(4,18,34,75),outline=(255,255,255,38),width=2)
    for i,(label,val) in enumerate(metrics[:12]): _metric(d,120+(i%4)*340,568+(i//4)*62,label,val,accent)
    # hourly strip
    _txt(d,(92,748),"NEXT HOURS",17,accent,True)
    hourly=weather.hourly_forecast[:8]
    if hourly:
        for i,row in enumerate(hourly):
            x=105+i*172; stamp=str(row.get("time") or "").split("T")[-1][:5]; code=int(row.get("weather_code") or 0)
            _txt(d,(x,775),stamp,17,(205,225,238)); _icon(d,x+35,825,code,.45,night)
            temp=row.get("temperature_2m"); _txt(d,(x+35,870),f"{round(temp)}°" if temp is not None else "—",20,(255,255,255),True,anchor="ma")
            prob=row.get("precipitation_probability");
            if prob is not None: _txt(d,(x+35,900),f"{round(prob)}%",15,accent,anchor="ma")
    # daily cards along bottom right / replaces hourly if API omitted it
    if weather.daily_forecast:
        _txt(d,(92,932),"7-DAY OUTLOOK",15,(190,215,232),True)
        for i,row in enumerate(weather.daily_forecast[:7]):
            if i>=7: break
            x=300+i*170; day=str(row.get("date") or "")[5:]; code=int(row.get("code") or 0)
            _txt(d,(x,932),day,15,(210,228,240),anchor="ma"); _icon(d,x,956,code,.25,night)
            hi,lo=row.get("high"),row.get("low")
            _txt(d,(x+45,950),f"{round(hi) if hi is not None else '—'}° / {round(lo) if lo is not None else '—'}°",15,(255,255,255),anchor="lm")
    if weather.sunrise or weather.sunset:
        sun="  •  ".join(x for x in (f"Sunrise {weather.sunrise}" if weather.sunrise else "",f"Sunset {weather.sunset}" if weather.sunset else "") if x)
        _txt(d,(1508,932),sun,17,(211,229,240),anchor="ra")
    out=io.BytesIO(); im.save(out,"PNG",optimize=True); return out.getvalue()
