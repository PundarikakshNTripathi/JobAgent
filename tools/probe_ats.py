"""Probe public ATS JSON endpoints for a list of candidate company slugs.
Usage: python probe_ats.py > ats_probe.json"""
import json, sys, concurrent.futures as cf, urllib.request
class _R:
    def __init__(s,code,body): s.status_code=code; s._b=body
    def json(s): return json.loads(s._b)
class S:
    @staticmethod
    def get(u,timeout=12):
        try:
            rq=urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0 job-agent-probe"})
            with urllib.request.urlopen(rq,timeout=timeout) as r: return _R(r.status,r.read().decode("utf-8","replace"))
        except urllib.error.HTTPError as e: return _R(e.code,"")
CANDS = """anthropic openai deepmind googledeepmind xai databricks scaleai stripe anduril cerebrassystems cerebras mistral mistralai cohere perplexityai perplexity
together togetherai fireworksai fireworks modal modallabs anyscale baseten groq sambanovasystems etched lambda lambdalabs coreweave runwayml runway huggingface
langchain anysphere cursor cognition cognitionai elevenlabs characterai character thinkingmachines thinkingmachineslab reflectionai physicalintelligence
apolloresearch metr farai goodfire transluce conjecture eleuthera redwoodresearch safesuperintelligence ssi adept inflection stabilityai liquidai liquid
midjourney pika lumalabs luma harvey sierra decagon glean writer hebbia abridge hippocratic notion figma ramp brex rippling vercel supabase
nvidia amd intel qualcomm tenstorrent d-matrix sima sambanova graphcore cerebras lightmatter rivosinc rivos groq mythic untether axelera esperanto
imbue contextual contextualai adeptai sakana sakanaai moonshot deepseek zhipu allenai ai2 allenInstitute arcinstitute eleutherai 
openrouter replicate neon planetscale temporal posthog weightsandbiases wandb coreweave nebius crusoe lambda vast  tensorwave 
cartesia hume humeai decart genmo ideogram black-forest-labs bfl krea poolside magic magicdev codeium windsurf replit lovable bolt stackblitz 
snorkel snorkelai labelbox surgeai surge mercor turing invisible handshake outlier appliedintuition waymo nuro aurora zoox wayve skildai skild figureai figure 1x 1xtech apptronik agilityrobotics bostondynamics
jane-street janestreet citadel hudson-river-trading hrt towerresearch jumptrading imc optiver drw twosigma deshaw
razorpay phonepe cred zepto meesho flipkart swiggy groww sarvam sarvamai krutrim ola olakrutrim
openpipe weaviate pinecone qdrant chroma lancedb anthropicpbc vllm inferact sglang radixark thinkymachines unsloth axolotl
mosaicml lightningai lightning gradient nomicai nomic jina voyageai cohereai adaptive adaptiveml 
tesla spacex neuralink openevidence glean sourcegraph warp zed linear retool airtable clickhouse cockroach snowflake confluent datadog cloudflare
""".split()
CANDS = sorted(set(CANDS))
def probe(slug):
    out = {}
    tests = {
      "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
      "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
      "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
      "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
      "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
      "recruitee": f"https://{slug}.recruitee.com/api/offers/",
    }
    for k,u in tests.items():
        try:
            r = S.get(u, timeout=12)
            if r.status_code != 200: continue
            j = r.json()
            if k=="greenhouse": n=len(j.get("jobs",[]))
            elif k=="lever": n=len(j) if isinstance(j,list) else 0
            elif k=="ashby": n=len(j.get("jobs",[]))
            elif k=="workable": n=len(j.get("jobs",[]))
            elif k=="smartrecruiters": n=j.get("totalFound",0)
            elif k=="recruitee": n=len(j.get("offers",[]))
            if n>0: out[k]=n
        except Exception: pass
    return slug,out
with cf.ThreadPoolExecutor(16) as ex:
    res = dict(ex.map(probe, CANDS))
print(json.dumps({k:v for k,v in res.items() if v}, indent=1))
