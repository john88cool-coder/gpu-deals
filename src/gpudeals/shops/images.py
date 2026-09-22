"""Read store-provided image URLs; never synthesize product photos."""
from urllib.parse import urljoin, urlsplit


def safe_image(value, base):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urljoin(base, value.strip())
        return url if urlsplit(url).scheme in ('https', 'http') and urlsplit(url).netloc else None
    except ValueError:
        return None


def card_image(node, base):
    for image in node.css('[itemprop="image"], img'):
        attrs = image.attributes
        for key in ('data-src', 'data-original', 'content', 'src', 'href'):
            url = safe_image(attrs.get(key), base)
            if url:
                return url
    return None


def json_image(value, base):
    if isinstance(value, list):
        return next((url for item in value if (url := json_image(item, base))), None)
    if isinstance(value, dict):
        return next((url for key in ('url', 'src', 'large', 'medium', 'small')
                     if (url := json_image(value.get(key), base))), None)
    return safe_image(value, base)
