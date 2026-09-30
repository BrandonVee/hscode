"""通过 MCP HTTP 调用四个工具及资源，验证部署后的真实服务。"""
import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def verify(url: str, *, allow_no_vectors: bool = False) -> None:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"search_hs_code", "get_hs_detail", "list_chapter_codes",
                             "compare_across_countries"}, names
            for name, arguments in [
                ("search_hs_code", {"keyword": "龙虾"}),
                ("search_hs_code", {"keyword": "lobster", "country": "US"}),
                ("search_hs_code", {"keyword": "龙虾", "country": "US"}),
                ("search_hs_code", {"keyword": "保温杯"}),
                ("get_hs_detail", {"code": "03061200"}),
                ("list_chapter_codes", {"chapter": "03"}),
                ("compare_across_countries", {"code": "03061200"}),
            ]:
                result = await session.call_tool(name, arguments)
                assert not result.is_error, result
                payload = result.structured_content
                if payload is None:
                    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
                assert payload.get("count", payload.get("found", False)), payload
                if arguments.get("keyword") == "保温杯" and not allow_no_vectors:
                    assert payload["match"] == "vector", payload
                print(f"通过：{name} {arguments}")
            resource = await session.read_resource("hs://dataset-info")
            assert resource.contents
            print("MCP HTTP 工具及资源验证通过" + ("（允许全文检索模式）" if allow_no_vectors else "，语义检索已验证"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp/hscode")
    parser.add_argument("--allow-no-vectors", action="store_true", help="向量尚未生成时验证全文检索服务")
    args = parser.parse_args()
    asyncio.run(verify(args.url, allow_no_vectors=args.allow_no_vectors))
