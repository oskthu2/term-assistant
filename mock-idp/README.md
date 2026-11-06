# Mockad inloggningstjänst (Keycloak)

För att prova administrationsverktygets OIDC-läge lokalt. **Bara för test**: demolösenord,
http och `start-dev`.

```bash
cd mock-idp && docker compose up -d          # http://localhost:8180, admin/admin i konsolen
```

Realmen `term` har klienten `term-admin` (konfidentiell, PKCE, gruppclaim `groups`) och
fem användare med lösenordet `demo`:

| Användare | Grupp i Keycloak | Roller i verktyget |
|---|---|---|
| anna | kodverk-redaktorer | läsare, redaktör |
| bertil | kodverk-granskare | läsare, granskare |
| cecilia | kodverk-lasare | läsare |
| petra | kodverk-publicerare, kodverk-granskare | läsare, publicerare, granskare |
| ada | kodverk-administratorer | läsare, administratör |

Lägg in `auth`-delen från [`auth-exempel.json`](auth-exempel.json) i arbetsytans
`settings.json` och starta verktyget:

```bash
export TERM_SESSION_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
export TERM_OIDC_CLIENT_SECRET=demo-klienthemlighet-byt-i-drift
term-inlasning admin --workspace arbetsyta --port 8000     # öppna http://localhost:8000
```

Logga in som anna och skapa en releasekandidat, som bertil och godkänn den, som petra och
publicera. Som ada syns alla som loggat in under **Användare och roller**, och där kan roller
tilldelas.

Flödet förutsätter att webbläsaren når både `localhost:8000` och `localhost:8180`, alltså lokalt
eller med VS Code på datorn. I Codespaces i webbläsaren har porterna andra adresser; använd
demoläget där.
