==> Cloning from https://github.com/alanalways/line
==> Checking out commit fc93037aff43ec78ec8e4f47a5bf39bb9415a54c in branch main
==> Downloading cache...
==> Transferred 104MB in 7s. Extraction took 4s.
==> Installing Python version 3.10.14...
==> Using Python version 3.10.14 via environment variable PYTHON_VERSION
==> Docs on specifying a Python version: https://render.com/docs/python-version
==> Using Poetry version 1.7.1 (default)
==> Docs on specifying a Poetry version: https://render.com/docs/poetry-version
==> Running build command 'pip install -r requirements.txt'...
Collecting flask==2.0.1
  Using cached Flask-2.0.1-py3-none-any.whl (94 kB)
Collecting werkzeug<3.0,>=2.0
  Using cached werkzeug-2.3.8-py3-none-any.whl (242 kB)
Collecting line-bot-sdk==2.3.0
  Using cached line_bot_sdk-2.3.0-py2.py3-none-any.whl (88 kB)
Collecting groq==0.9.0
  Downloading groq-0.9.0-py3-none-any.whl (103 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 103.5/103.5 kB 4.2 MB/s eta 0:00:00
Collecting psycopg2-binary
  Using cached psycopg2_binary-2.9.10-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (3.0 MB)
Collecting python-dotenv>=1.0.0
  Using cached python_dotenv-1.1.0-py3-none-any.whl (20 kB)
Collecting gunicorn
  Using cached gunicorn-23.0.0-py3-none-any.whl (85 kB)
Collecting click>=7.1.2
  Using cached click-8.1.8-py3-none-any.whl (98 kB)
Collecting itsdangerous>=2.0
  Using cached itsdangerous-2.2.0-py3-none-any.whl (16 kB)
Collecting Jinja2>=3.0
  Using cached jinja2-3.1.6-py3-none-any.whl (134 kB)
Collecting requests>=2.0
  Using cached requests-2.32.3-py3-none-any.whl (64 kB)
Collecting aiohttp>=3.7.4
  Using cached aiohttp-3.11.18-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.6 MB)
Collecting future
  Using cached future-1.0.0-py3-none-any.whl (491 kB)
Collecting sniffio
  Using cached sniffio-1.3.1-py3-none-any.whl (10 kB)
Collecting httpx<1,>=0.23.0
  Using cached httpx-0.28.1-py3-none-any.whl (73 kB)
Collecting pydantic<3,>=1.9.0
  Using cached pydantic-2.11.4-py3-none-any.whl (443 kB)
Collecting distro<2,>=1.7.0
  Using cached distro-1.9.0-py3-none-any.whl (20 kB)
Collecting anyio<5,>=3.5.0
  Using cached anyio-4.9.0-py3-none-any.whl (100 kB)
Collecting typing-extensions<5,>=4.7
  Using cached typing_extensions-4.13.2-py3-none-any.whl (45 kB)
Collecting MarkupSafe>=2.1.1
  Using cached MarkupSafe-3.0.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (20 kB)
Collecting packaging
  Using cached packaging-25.0-py3-none-any.whl (66 kB)
Collecting multidict<7.0,>=4.5
  Using cached multidict-6.4.3-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (219 kB)
Collecting async-timeout<6.0,>=4.0
  Using cached async_timeout-5.0.1-py3-none-any.whl (6.2 kB)
Collecting aiohappyeyeballs>=2.3.0
  Using cached aiohappyeyeballs-2.6.1-py3-none-any.whl (15 kB)
Collecting yarl<2.0,>=1.17.0
  Using cached yarl-1.20.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (333 kB)
Collecting attrs>=17.3.0
  Using cached attrs-25.3.0-py3-none-any.whl (63 kB)
Collecting frozenlist>=1.1.1
  Using cached frozenlist-1.6.0-cp310-cp310-manylinux_2_5_x86_64.manylinux1_x86_64.manylinux_2_17_x86_64.manylinux2014_x86_64.whl (287 kB)
Collecting propcache>=0.2.0
  Using cached propcache-0.3.1-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (206 kB)
Collecting aiosignal>=1.1.2
  Using cached aiosignal-1.3.2-py2.py3-none-any.whl (7.6 kB)
Collecting idna>=2.8
  Using cached idna-3.10-py3-none-any.whl (70 kB)
Collecting exceptiongroup>=1.0.2
  Using cached exceptiongroup-1.2.2-py3-none-any.whl (16 kB)
Collecting httpcore==1.*
  Using cached httpcore-1.0.9-py3-none-any.whl (78 kB)
Collecting certifi
  Using cached certifi-2025.4.26-py3-none-any.whl (159 kB)
Collecting h11>=0.16
  Using cached h11-0.16.0-py3-none-any.whl (37 kB)
Collecting typing-inspection>=0.4.0
  Using cached typing_inspection-0.4.0-py3-none-any.whl (14 kB)
Collecting pydantic-core==2.33.2
  Using cached pydantic_core-2.33.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.0 MB)
Collecting annotated-types>=0.6.0
  Using cached annotated_types-0.7.0-py3-none-any.whl (13 kB)
Collecting charset-normalizer<4,>=2
  Using cached charset_normalizer-3.4.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (149 kB)
Collecting urllib3<3,>=1.21.1
  Using cached urllib3-2.4.0-py3-none-any.whl (128 kB)
Installing collected packages: urllib3, typing-extensions, sniffio, python-dotenv, psycopg2-binary, propcache, packaging, MarkupSafe, itsdangerous, idna, h11, future, frozenlist, exceptiongroup, distro, click, charset-normalizer, certifi, attrs, async-timeout, annotated-types, aiohappyeyeballs, werkzeug, typing-inspection, requests, pydantic-core, multidict, Jinja2, httpcore, gunicorn, anyio, aiosignal, yarl, pydantic, httpx, flask, groq, aiohttp, line-bot-sdk
Successfully installed Jinja2-3.1.6 MarkupSafe-3.0.2 aiohappyeyeballs-2.6.1 aiohttp-3.11.18 aiosignal-1.3.2 annotated-types-0.7.0 anyio-4.9.0 async-timeout-5.0.1 attrs-25.3.0 certifi-2025.4.26 charset-normalizer-3.4.2 click-8.1.8 distro-1.9.0 exceptiongroup-1.2.2 flask-2.0.1 frozenlist-1.6.0 future-1.0.0 groq-0.9.0 gunicorn-23.0.0 h11-0.16.0 httpcore-1.0.9 httpx-0.28.1 idna-3.10 itsdangerous-2.2.0 line-bot-sdk-2.3.0 multidict-6.4.3 packaging-25.0 propcache-0.3.1 psycopg2-binary-2.9.10 pydantic-2.11.4 pydantic-core-2.33.2 python-dotenv-1.1.0 requests-2.32.3 sniffio-1.3.1 typing-extensions-4.13.2 typing-inspection-0.4.0 urllib3-2.4.0 werkzeug-2.3.8 yarl-1.20.0
WARNING: There was an error checking the latest version of pip.
==> Uploading build...
==> Uploaded in 6.6s. Compression took 55.0s
==> Build successful 🎉
==> Deploying...
==> Running 'gunicorn app:app --timeout 120 --log-level info'
[2025-05-04 15:31:22,072] INFO in app: Line Bot SDK v2 初始化成功。
[2025-05-04 15:31:22,073] ERROR in app: 無法初始化 Groq client: Client.__init__() got an unexpected keyword argument 'proxies'
==> Running 'gunicorn app:app --timeout 120 --log-level info'
[2025-05-04 15:31:37,361] INFO in app: Line Bot SDK v2 初始化成功。
[2025-05-04 15:31:37,361] ERROR in app: 無法初始化 Groq client: Client.__init__() got an unexpected keyword argument 'proxies'
==> No open ports detected, continuing to scan...
==> Docs on specifying a port: https://render.com/docs/web-services#port-binding
==> Docs on specifying a port: https://render.com/docs/web-services#port-binding
==> Running 'gunicorn app:app --timeout 120 --log-level info'
[2025-05-04 15:32:03,816] INFO in app: Line Bot SDK v2 初始化成功。
[2025-05-04 15:32:03,816] ERROR in app: 無法初始化 Groq client: Client.__init__() got an unexpected keyword argument 'proxies'
