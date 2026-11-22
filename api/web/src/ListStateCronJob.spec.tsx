import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { apiConfigurationDefault } from "./ApiConfigurationProvider";
import { ListState } from "./ListState";

import { describe, expect, it } from "vitest";
import {
  mockCronJobActive,
  mockCronJobSuspended,
  mockUnsupported,
} from "./ListState.mock";
import { computeState } from "./state";

describe(`ListState CronJob and unsupported rendering`, () => {
  it(`shows a suspended CronJob as suspended, not as a replica ratio`, () => {
    render(
      <ListState
        apiState={mockCronJobSuspended}
        apiConfigurationState={apiConfigurationDefault}
        priceByMonth={0}
        locale="FR-fr"
      />,
    );
    expect(screen.queryByText("nightly-report")).toBeTruthy();
    expect(screen.queryByText("suspended")).toBeTruthy();
    expect(screen.queryByText("0/0")).toBeNull();
    expect(screen.queryByText("Stopped")).toBeTruthy();
  });

  it(`shows an active CronJob as active`, () => {
    render(
      <ListState
        apiState={mockCronJobActive}
        apiConfigurationState={apiConfigurationDefault}
        priceByMonth={0}
        locale="FR-fr"
      />,
    );
    expect(screen.queryByText("active")).toBeTruthy();
    expect(screen.queryByText("1/1")).toBeNull();
  });

  it(`flags an unsupported workload instead of inventing replicas`, () => {
    render(
      <ListState
        apiState={mockUnsupported}
        apiConfigurationState={apiConfigurationDefault}
        priceByMonth={0}
        locale="FR-fr"
      />,
    );
    expect(screen.queryByText("Not supported")).toBeTruthy();
    expect(screen.queryByText("not supported")).toBeTruthy();
  });
});

describe(`computeState with scheduled controllers`, () => {
  it(`treats a suspended opted-in CronJob as stopped`, () => {
    expect(computeState(mockCronJobSuspended.workloads)).toBe("STOPPED");
  });

  it(`treats an active opted-in CronJob as started`, () => {
    expect(computeState(mockCronJobActive.workloads)).toBe("STARTED");
  });

  it(`ignores unsupported workloads when computing state`, () => {
    expect(computeState(mockUnsupported.workloads)).toBe("STARTED");
  });
});
