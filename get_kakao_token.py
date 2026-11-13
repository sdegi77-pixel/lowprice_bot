import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = pathlib.Path(r"c:\app_project\lowpirce\.env")

def parse_env(path: pathlib.Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def upsert_env(path: pathlib.Path, key: str, value: str):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def exchange_code(code: str, redirect_uri: str = "http://localhost"):
    env = parse_env(ENV_PATH)
    client_id = env.get("KAKAO_REST_API_KEY")
    client_secret = env.get("KAKAO_CLIENT_SECREN") or env.get("KAKAO_CLIENT_SECRET")
    
    if not client_id:
        print("[ERROR] .env 파일에 KAKAO_REST_API_KEY가 없습니다.", file=sys.stderr)
        return False
        
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        form["client_secret"] = client_secret
        
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            refresh_token = data.get("refresh_token")
            if not refresh_token:
                print(f"[ERROR] 응답에 refresh_token이 없습니다: {data}", file=sys.stderr)
                return False
                
            upsert_env(ENV_PATH, "KAKAO_REFRESH_TOKEN", refresh_token)
            upsert_env(ENV_PATH, "KADKO_ REFRESH_TOKEN", refresh_token)
            print(f"[SUCCESS] 리프레시 토큰 발급 및 .env 등록 완료!")
            print(f"Token Type: {data.get('token_type')}")
            print(f"Refresh Token Expire: {data.get('refresh_token_expires_in')}초")
            return True
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace")
        print(f"[HTTP Error {exc.code}] {err_body}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[Error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        code = sys.argv[1]
        uri = sys.argv[2] if len(sys.argv) > 2 else "http://localhost"
        sys.exit(0 if exchange_code(code, uri) else 1)
    else:
        print("Usage: python get_kakao_token.py <AUTH_CODE> [REDIRECT_URI]")
        sys.exit(1)
