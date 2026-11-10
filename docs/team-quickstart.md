# Join Evalve

Your operator will send you an invitation link and the Evalve dashboard address. All you need to sign in and view demo traces is a browser.

1. Open your invitation link and set your password.
2. Sign in at the Evalve dashboard address. Bookmark it for your next visit.
3. Open **Agents**, select the demo agent, and open a session to view its trace.

## Connect a demo agent

If your agent already records with Kitaru, connect it to Evalve using your own API key.

1. In the dashboard, open **Settings → API keys → Create API key**. Give the key a name you will recognize and copy it when it appears.
2. In your agent's existing environment or secret settings, set `KITARU_API_URL` to the full Evalve `https://` address and `KITARU_API_KEY` to the key you copied. If your integration takes a Kitaru agent or version ID, use the one registered in Evalve.
3. Run the agent once with synthetic inputs. In Evalve, open the agent and confirm the new session appears with its trace.

If this is your agent's first Kitaru integration, follow the adapter setup for its framework, then use the Evalve URL and key above. [Kitaru recording guide](https://docs.zenml.io/kitaru/adapters/record-in-production)

If the link expires, the dashboard is unavailable, or your trace does not appear, contact the operator with the agent name and approximate run time. Keep passwords and API keys out of the message.
