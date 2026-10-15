import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { setSession } from "../api/session";
import { describeError } from "../lib/errors";

export function SignIn({ notice }: { notice?: string }) {
  const personas = useQuery({ queryKey: ["personas"], queryFn: api.personas, retry: 1 });
  const signIn = useMutation({
    mutationFn: (customerId: string) => api.signIn(customerId),
    onSuccess: (session) => setSession(session),
  });

  return (
    <section className="card signin" aria-labelledby="signin-title" data-testid="signin">
      <h2 id="signin-title">Demo sign-in, synthetic customer</h2>
      <p>
        Choose a synthetic customer. Their ALDO bot and the ALDO Returns Agent can only see that customer&apos;s own data, and
        everything here is simulated.
      </p>
      {notice && (
        <p className="alert alert-info" role="status">
          {notice}
        </p>
      )}
      {personas.isPending && <p role="status">Loading customers...</p>}
      {personas.isError && (
        <div className="alert alert-error" role="alert">
          <p>{describeError(personas.error).message}</p>
          <button type="button" className="btn" onClick={() => void personas.refetch()}>
            Try again
          </button>
        </div>
      )}
      {personas.data && (
        <ul className="persona-list" aria-label="Synthetic customers">
          {personas.data.map((p) => (
            <li key={p.customer_id} className="persona">
              <button
                type="button"
                className="persona-btn"
                disabled={signIn.isPending}
                aria-describedby={p.description ? `persona-desc-${p.customer_id}` : undefined}
                onClick={() => signIn.mutate(p.customer_id)}
              >
                Sign in as {p.display_name}
              </button>
              {p.description && (
                <p className="persona-desc" id={`persona-desc-${p.customer_id}`}>
                  {p.description}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {signIn.isError && (
        <p className="alert alert-error" role="alert">
          {describeError(signIn.error).message}
        </p>
      )}
    </section>
  );
}
