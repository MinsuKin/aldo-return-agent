import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { DELIVERY_DELAY_MS } from "../../../config";
import type { Delivery } from "../../../hooks/useConversation";
import { colorWord, friendlyDate } from "../../../lib/format";
import { normalizePurchase } from "../../../lib/normalize";
import { Icon } from "../../Icon";
import { CardFrame, ProductPhoto } from "./shared";

interface Props {
  orderId: string;
  sizeRequestId?: string;
  delivery?: Delivery;
  onRetryDelivery: (orderId: string) => void;
}

/** "Order placed", then 5 seconds of "Out for delivery", then "Delivered" (addendum 11.1 D10). */
export function OrderCard({ orderId, sizeRequestId, delivery, onRetryDelivery }: Props) {
  const query = useQuery({
    queryKey: ["purchase", sizeRequestId],
    enabled: !!sizeRequestId,
    queryFn: () => api.purchase(sizeRequestId as string),
    retry: 2,
  });
  const view = useMemo(() => (sizeRequestId && query.data ? normalizePurchase(sizeRequestId, query.data) : undefined), [sizeRequestId, query.data]);

  const status = delivery?.status;
  const how = view?.optionLabel
    ? `${view.optionLabel}${view.locationName ? ` · ${view.locationName}` : ""}${view.etaDate ? ` · ${view.optionType === "store_pickup" ? "ready" : "arrives"} ${friendlyDate(view.etaDate)}` : ""}`
    : undefined;

  return (
    <CardFrame
      title="Order placed"
      titleId={`order-title-${orderId}`}
      testId="order-card"
      status={status ?? "placed"}
      tone="success"
      meta={<span className="mono muted small" data-testid="order-id">{orderId}</span>}
    >
      {view && (
        <div className="product-row">
          <ProductPhoto name={view.productName} color={view.color} size="hero" />
          <div>
            <p className="product-name">
              {view.productName ?? "ALDO style"}
              {view.color ? `, ${colorWord(view.color)}` : ""}
            </p>
            <p className="product-size">
              Size <strong>{view.size ?? "?"}</strong>
            </p>
          </div>
        </div>
      )}
      {how && <p className="order-how">{how}</p>}

      {status === "out" && (
        <div className="delivery" data-testid="delivery-status" data-state="out">
          <p className="delivery-line">
            <Icon name="truck" size={18} /> <strong>Out for delivery</strong>
          </p>
          <div className="delivery-bar" aria-hidden="true">
            <span style={{ animationDuration: `${DELIVERY_DELAY_MS}ms` }} />
          </div>
        </div>
      )}
      {status === "delivered" && (
        <p className="delivery-line delivery-done" data-testid="delivery-status" data-state="delivered">
          <Icon name="delivered" size={18} /> <strong>Delivered</strong>
        </p>
      )}
      {status === "error" && (
        <div className="alert alert-error" role="alert" data-testid="delivery-status" data-state="error">
          <p>{delivery?.error ?? "Could not update the delivery."}</p>
          <button type="button" className="btn btn-small" onClick={() => onRetryDelivery(orderId)}>
            Try again
          </button>
        </div>
      )}
    </CardFrame>
  );
}
