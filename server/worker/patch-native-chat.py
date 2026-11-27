"""Hide sales-review control lines in the pinned rc.5 chat renderer."""

import argparse
import hashlib
from pathlib import Path


UPSTREAM_SHA256 = "eef53e82b8cd584d2f0e543a4a93395f9d90df3bba1218891bd99d07d788de5c"
RELEASE_SHA256 = "61c1664bac123ebd3227f398e9b8d7480c3fdc062cfad7ca59039a4893eef604"

# Keep this projection inside AssistantMarkdown. Other message views and
# the conversation's order-card reader still receive the complete event.
PROJECTION = br'''const reviewMarker=/^[\t ]*<!-- maimi-sales-review-v1:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12} -->[\t ]*(?:\r?\n|$)/gim;
const withoutReviewMarkers=blocks.flatMap(block=>{
  if(block.kind!=="text"||!(/<!-- maimi-sales-review-v1:/i.test(block.text)))return [block];
  const text=block.text.replace(reviewMarker,"");
  if(text===block.text)return [block];
  return text.trim()?[{...block,text}]:[];
});
if(!interrupted&&blocks.length&&!withoutReviewMarkers.length)return null;
blocks=withoutReviewMarkers;'''


def render(raw):
    digest = hashlib.sha256(raw).hexdigest()
    if digest == RELEASE_SHA256:
        return raw
    if digest != UPSTREAM_SHA256:
        raise ValueError(f"Unsupported native chat SHA256 {digest}. Requalify the renderer for this DSH version.")
    start = raw.index(b"function AssistantMarkdown(")
    end = raw.index(b"const rendered=[];", start)
    region = raw[start:end]
    anchor = b"const last=blocks.length-1;"
    if region.count(anchor) != 1:
        raise ValueError("Expected exactly one block index in AssistantMarkdown")
    result = raw[:start] + region.replace(anchor, PROJECTION + anchor) + raw[end:]
    if hashlib.sha256(result).hexdigest() != RELEASE_SHA256:
        raise ValueError("Patched native chat differs from the qualified release")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render(args.input.read_bytes())
    if not args.output.exists() or args.output.read_bytes() != result:
        with args.output.open("xb") as output:
            output.write(result)
    print(RELEASE_SHA256)


if __name__ == "__main__":
    main()
