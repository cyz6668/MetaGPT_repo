import asyncio
import os
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional
import requests
import aiohttp
import discord
from aiocron import crontab
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from pytz import BaseTzInfo

from metagpt.actions.action import Action
#from metagpt.config import CONFIG
from metagpt.logs import logger
from metagpt.roles import Role
from metagpt.schema import Message
from urllib.parse import urljoin
# fix SubscriptionRunner not fully defined
from metagpt.environment import Environment as _  # noqa: F401
import re


# 订阅模块，可以from metagpt.subscription import SubscriptionRunner导入，这里贴上代码供参考
class SubscriptionRunner(BaseModel):
    """A simple wrapper to manage subscription tasks for different roles using asyncio.
    Example:
        >>> import asyncio
        >>> from metagpt.subscription import SubscriptionRunner
        >>> from metagpt.roles import Searcher
        >>> from metagpt.schema import Message
        >>> async def trigger():
        ...     while True:
        ...         yield Message("the latest news about OpenAI")
        ...         await asyncio.sleep(3600 * 24)
        >>> async def callback(msg: Message):
        ...     print(msg.content)
        >>> async def main():
        ...     pb = SubscriptionRunner()
        ...     await pb.subscribe(Searcher(), trigger(), callback)
        ...     await pb.run()
        >>> asyncio.run(main())
    """

    tasks: Dict[Role, asyncio.Task] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    async def subscribe(
        self,
        role: Role,
        trigger: AsyncGenerator[Message, None],
        callback: Callable[
            [
                Message,
            ],
            Awaitable[None],
        ],
    ):
        """Subscribes a role to a trigger and sets up a callback to be called with the role's response.
        Args:
            role: The role to subscribe.
            trigger: An asynchronous generator that yields Messages to be processed by the role.
            callback: An asynchronous function to be called with the response from the role.
        """
        loop = asyncio.get_running_loop()

        async def _start_role():
            async for msg in trigger:
                resp = await role.run(msg)
                await callback(resp)

        self.tasks[role] = loop.create_task(_start_role(), name=f"Subscription-{role}")

    async def unsubscribe(self, role: Role):
        """Unsubscribes a role from its trigger and cancels the associated task.
        Args:
            role: The role to unsubscribe.
        """
        task = self.tasks.pop(role)
        task.cancel()

    async def run(self, raise_exception: bool = True):
        """Runs all subscribed tasks and handles their completion or exception.
        Args:
            raise_exception: _description_. Defaults to True.
        Raises:
            task.exception: _description_
        """
        while True:
            for role, task in self.tasks.items():
                if task.done():
                    if task.exception():
                        if raise_exception:
                            raise task.exception()
                        logger.opt(exception=task.exception()).error(
                            f"Task {task.get_name()} run error"
                        )
                    else:
                        logger.warning(
                            f"Task {task.get_name()} has completed. "
                            "If this is unexpected behavior, please check the trigger function."
                        )
                    self.tasks.pop(role)
                    break
            else:
                await asyncio.sleep(1)


# Actions 的实现
TRENDING_ANALYSIS_PROMPT = """# Requirements
You are a huggingface paper Analyst, aiming to provide users with insightful and personalized recommendations based on the latest
huggingface popular papers. And you need to provide more details of the papers.

# The title about Today's huggingface Trending
## Today's Trends: Uncover the Hottest huggingface Papers Today! Discover key domains capturing developers' attention. From ** to **, witness the popular papers like never before.
## The document detail of the paper: print out the  details.
---
# Format Example

```
# [Title]

## Today's Trends
Key areas of interest include **, ** and **.
The top popular papers are Paper1 and Paper2.

## The documents of papers
1. [Paper](https://huggingface.co/xx/paper1):[provide the details of the paper ].
...
```
---
# Huggingface Trending
{trending}
"""


class CrawlOSSTrending(Action):

    def extract_paper_id(self, link):
        """从链接中提取论文ID"""
        # 链接格式: /papers/2512.13604
        match = re.search(r'/papers/(\d+\.\d+)', link)
        if match:
            return match.group(1)
        return None
    
    def extract_authors(self, article):
        """提取作者信息"""
        authors = []
        try:
            author_elements = article.find_all('li', class_=lambda x: x and 'bg-linear-to-br' in x)
            for author_elem in author_elements[:5]:  # 最多取前5个作者
                author_name = author_elem.get('title', '')
                if author_name:
                    authors.append(author_name)
        except:
            pass
        
        # 尝试从作者数量文本中提取
        try:
            author_count_elem = article.find('li', class_='text-xs')
            if author_count_elem:
                author_text = author_count_elem.get_text(strip=True)
                # 匹配格式: "· 10 authors"
                match = re.search(r'(\d+)\s*authors', author_text)
                if match:
                    return {
                        'names': authors,
                        'count': int(match.group(1))
                    }
        except:
            pass
        
        return {
            'names': authors,
            'count': len(authors)
        }
    
    def extract_likes(self, article):
        """提取点赞数"""
        try:
            like_elem = article.find('div', class_='leading-none')
            if like_elem:
                likes = like_elem.get_text(strip=True)
                return int(likes) if likes.isdigit() else 0
        except:
            return 0
    

    async def run(self, url: str = "https://huggingface.co/papers?date=2025-12-15"):
        async with aiohttp.ClientSession() as client:
            async with client.get(url, proxy="http://10.162.37.16:8128") as response:
                response.raise_for_status()
                html = await response.text()

        soup = BeautifulSoup(html, "html.parser")

        papers = []
        
        # 找到所有文章元素
        article_elements = soup.find_all('article', class_='relative')
        
        for article in article_elements:
            try:
                # 提取文章标题和链接
                title_element = article.find('h3').find('a')
                if title_element:
                    title = title_element.get_text(strip=True)
                    relative_link = title_element.get('href')
                    
                    # 构建完整链接
                    if relative_link:
                        if url:
                            full_link = urljoin(url, relative_link)
                        else:
                            full_link = f"https://huggingface.co{relative_link}"
                        
                        # 提取论文ID（从链接中）
                        paper_id = self.extract_paper_id(relative_link)
                        
                        # 提取其他可能的信息
                        authors = self.extract_authors(article)
                        likes = self.extract_likes(article)
                        
                        papers.append({
                            'title': title,
                            'link': full_link,
                            'paper_id': paper_id,
                            'authors': authors,
                            'likes': likes
                        })
            except Exception as e:
                print(f"解析文章时出错: {e}")
                continue
        
        return papers


class AnalysisOSSTrending(Action):
    async def run(self, trending: Any):
        return await self._aask(TRENDING_ANALYSIS_PROMPT.format(trending=trending))


# Role实现
class OssWatcher(Role):
    def __init__(
        self,
        name="Codey",
        profile="OssWatcher",
        goal="Generate an insightful Huggingface Trending paper analysis report.",
        constraints="Only analyze based on the provided Huggingface Trending data.",
    ):
        super().__init__(name=name, profile=profile, goal=goal, constraints=constraints)
        self.set_actions([CrawlOSSTrending, AnalysisOSSTrending])
        self._set_react_mode(react_mode="by_order")

    async def _act(self) -> Message:
        logger.info(f"{self._setting}: ready to {self.rc.todo}")
        # By choosing the Action by order under the hood
        # todo will be first SimpleWriteCode() then SimpleRunCode()
        todo = self.rc.todo

        msg = self.get_memories(k=1)[0]  # find the most k recent messages
        result = await todo.run(msg.content)

        msg = Message(content=str(result), role=self.profile, cause_by=type(todo))
        self.rc.memory.add(msg)
        return msg


# Trigger
class HuggingfaceTrendingCronTrigger:
    def __init__(
        self,
        spec: str,
        tz: Optional[BaseTzInfo] = None,
        url: str = "https://huggingface.co/papers?date=2025-12-15",
    ) -> None:
        # self.crontab = crontab(spec, tz=tz)
        self.url = url

    def __aiter__(self):
        return self

    async def __anext__(self):
        # await self.crontab.next()
        return Message(content=self.url)


# callback
async def discord_callback(msg: Message):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, proxy="http://10.162.37.16:8128")
    token = os.environ["DISCORD_TOKEN"]
    channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
    async with client:
        await client.login(token)
        channel = await client.fetch_channel(channel_id)
        lines = []
        for i in msg.content.splitlines():
            if i.startswith(("# ", "## ", "### ")):
                if lines:
                    await channel.send("\n".join(lines))
                    lines = []
            lines.append(i)

        if lines:
            await channel.send("\n".join(lines))


class WxPusherClient:
    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = "http://wxpusher.zjiecode.com",
    ):
        self.base_url = base_url
        self.token = token or os.environ["WXPUSHER_TOKEN"]

    async def send_message(
        self,
        content,
        summary: Optional[str] = None,
        content_type: int = 1,
        topic_ids: Optional[list[int]] = None,
        uids: Optional[list[int]] = None,
        verify: bool = False,
        url: Optional[str] = None,
    ):
        payload = {
            "appToken": self.token,
            "content": content,
            "summary": summary,
            "contentType": content_type,
            "topicIds": topic_ids or [],
            "uids": uids or os.environ["WXPUSHER_UIDS"].split(","),
            "verifyPay": verify,
            "url": url,
        }
        url = f"{self.base_url}/api/send/message"
        return await self._request("POST", url, json=payload)

    async def _request(self, method, url, **kwargs):
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, **kwargs) as response:
                response.raise_for_status()
                return await response.json()


async def wxpusher_callback(msg: Message):
    client = WxPusherClient()
    await client.send_message(msg.content, content_type=3)


# 运行入口，
async def main(spec: str = "0 9 * * *", discord: bool = True, wxpusher: bool = True):
    callbacks = []
    if discord:
        callbacks.append(discord_callback)

    # if wxpusher:
    #     callbacks.append(wxpusher_callback)

    if not callbacks:

        async def _print(msg: Message):
            print(msg.content)

        callbacks.append(_print)

    async def callback(msg):
        await asyncio.gather(*(call(msg) for call in callbacks))

    runner = SubscriptionRunner()
    await runner.subscribe(OssWatcher(), HuggingfaceTrendingCronTrigger(spec), callback)
    await runner.run()


if __name__ == "__main__":
    import fire

    fire.Fire(main)
